"""客户端授权核心：验签、机器绑定、激活窗口、到期、防回拨、撤销令与回执。

设计要点见 docs/授权与打包方案.md：
- 私钥只在作者端；公钥内置（可被替换，因此不是唯一防线）。
- 状态在 %APPDATA% + 注册表双副本，覆盖安装不影响授权；读-改-写走
  ``state_store.update_state()`` 原子更新，避免看门狗与心跳互相覆盖。
- perpetual 不做时间校验，避免改系统时间误伤买断用户。
- timed 用可信日历时间 + **单调 high_water** + 心跳累计运行时长三重约束；
  seq 递增防重复导入重置计时。
- **首次激活与限时版每次启动都 fail-closed**：必须取得 NTP 共识且与本地
  时钟方向一致。间歇使用场景下「阻断 NTP + 回拨本地时间」不能再让已过期
  许可复活（第六轮 B-1）；perpetual 不做任何时间校验。
- high_water 是「历史最晚可信时间」，只增不减；自愈可以下调 last_seen，
  但绝不能下调 high_water，否则到期判据会被回拨绕过。
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time

from licensing import rsa_sign, fingerprint, clock, state_store
from licensing import public_key as _pk_module

LICENSE_VERSION = 1
APP_NAME = "ShootAtZombies"
VALID_TYPES = ("perpetual", "timed")
USAGE_SKEW_TOLERANCE = 3600      # 累计运行时长相对日历时长的允许偏差
MIN_IAT_SKEW = 3600              # 作者端/客户端都用 NTP，容差收敛为 1 小时
_MAX_INT = 2 ** 63 - 1

_PUBLIC_KEY_OVERRIDE = None
_REVOCATION_DIRS_OVERRIDE = None
_HEARTBEAT_STOP = threading.Event()
_HEARTBEAT_THREAD = None
_START_LOCK = threading.Lock()   # 串行化「检查是否已启动 → 赋值 → start」


def set_public_key(public: dict | None) -> None:
    global _PUBLIC_KEY_OVERRIDE
    _PUBLIC_KEY_OVERRIDE = public


def set_revocation_dirs(dirs: list[str] | None) -> None:
    global _REVOCATION_DIRS_OVERRIDE
    _REVOCATION_DIRS_OVERRIDE = dirs


def public_key() -> dict:
    if _PUBLIC_KEY_OVERRIDE is not None:
        return _PUBLIC_KEY_OVERRIDE
    return {"n": int(_pk_module.N), "e": int(_pk_module.E)}


def public_key_is_consistent() -> bool:
    """启动时自检：**始终**校验随包发布的内置公钥，不受测试注入影响。"""
    try:
        built_in = {"n": int(_pk_module.N), "e": int(_pk_module.E)}
        return rsa_sign.public_fingerprint(built_in) == str(_pk_module.FINGERPRINT)
    except Exception:
        return False


_REQUIRED_FIELDS = ("v", "app", "type", "order", "seq", "fp", "min_match",
                    "iat", "activate_before", "duration_seconds")
_INT_FIELDS = ("seq", "iat", "activate_before", "duration_seconds", "min_match")
_SCHEMA_ERROR = "许可字段缺失或格式错误"
_REVOKE_INT_FIELDS = ("seq", "iat")


def _strict_int(value) -> bool:
    # bool 是 int 子类；float('inf') 转 int 会抛 OverflowError，都必须挡掉
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= _MAX_INT)


def _payload_error(payload: dict) -> str | None:
    """严格 schema：类型白名单 + 范围上限，畸形 payload 一律拒绝而不是崩溃。"""
    if not isinstance(payload, dict):
        return _SCHEMA_ERROR
    for key in _REQUIRED_FIELDS:
        if key not in payload:
            return _SCHEMA_ERROR
    if not isinstance(payload["order"], str) or not payload["order"].strip():
        return _SCHEMA_ERROR
    if not fingerprint._valid_fp(payload["fp"]):
        return _SCHEMA_ERROR
    for key in _INT_FIELDS:
        if not _strict_int(payload[key]):
            return _SCHEMA_ERROR
    return None


def _revocation_error(payload: dict) -> str | None:
    """撤销令也要走 schema：签名保护的是作者，畸形字段仍可能让客户端崩溃。"""
    if not isinstance(payload, dict):
        return _SCHEMA_ERROR
    for key in ("order", "target_order", "challenge", "reason"):
        if not isinstance(payload.get(key), str):
            return _SCHEMA_ERROR
    if not payload["target_order"].strip():
        return _SCHEMA_ERROR
    for key in _REVOKE_INT_FIELDS:
        if not _strict_int(payload.get(key)):
            return _SCHEMA_ERROR
    return None


def revocation_dirs() -> list[str]:
    if _REVOCATION_DIRS_OVERRIDE is not None:
        return list(_REVOCATION_DIRS_OVERRIDE)
    candidates = []
    frozen = (getattr(sys, "frozen", False) or
              getattr(sys, "__compiled__", None) is not None)
    if frozen:
        # CWD 已被 launcher 切到 %APPDATA%，相对 argv[0] 会解析错；用
        # _MEIPASS 的父目录（即 {app}）定位随安装包下发的 revocations。
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(
                os.path.dirname(os.path.abspath(meipass)), "revocations"))
    else:
        try:
            base = os.path.dirname(os.path.abspath(sys.argv[0]))
            candidates.append(os.path.join(base, "revocations"))
        except Exception:
            pass
    candidates.append(os.path.join(state_store.app_dir(), "revocations"))
    return candidates


def build_request() -> str:
    return fingerprint.make_request()


def receipt_code(challenge: str, machine: str, revoked_at: int) -> str:
    """退款回执码：作者可用台账里的 challenge + 机器码复算核验。"""
    return hashlib.sha256(("%s|%s|%d" % (challenge, machine, int(revoked_at)))
                          .encode("utf-8")).hexdigest()[:16].upper()


def _high_water(state: dict | None) -> int:
    """历史最晚可信时间；老版本状态没有该字段时从 0 起算（首次可信复查补齐）。

    **不要**用 ``last_seen`` 兜底：旧状态里的 last_seen 可能被调快的本地时钟
    污染过，拿它当单调下界会把诚实客户永久锁死。
    """
    if not state:
        return 0
    return state_store.as_int(state.get("high_water", 0))


def _bonus_seconds(state: dict | None) -> int:
    if not state:
        return 0
    return max(0, state_store.as_int(state.get("bonus_seconds", 0)))


def _effective_duration(payload: dict, state: dict | None) -> int:
    """限时授权有效时长 = 当前码 duration + 叠加上的剩余 bonus。"""
    base = int(payload.get("duration_seconds") or 0)
    return max(0, base + _bonus_seconds(state))


def _remaining_seconds(state: dict, now: int) -> int:
    """当前仍有效许可的剩余秒数；耗尽/非限时/无效则 0。"""
    if not state or state.get("revoked"):
        return 0
    raw = state.get("token") or ""
    try:
        ok, payload, _ = rsa_sign.verify_token(raw, public_key())
    except Exception:
        return 0
    if not ok or not isinstance(payload, dict) or payload.get("type") != "timed":
        return 0
    activated_at = state_store.as_int(state.get("activated_at", 0))
    duration = _effective_duration(payload, state)
    if activated_at <= 0 or duration <= 0:
        return 0
    effective_now = max(now, _high_water(state))
    used = state_store.as_int(state.get("used_seconds", 0))
    calendar_left = activated_at + duration - effective_now
    usage_left = duration - used
    return max(0, min(calendar_left, usage_left))


def validate(token: str, state: dict | None = None,
             now: int | None = None) -> tuple[bool, dict | None, str]:
    """纯校验，不写状态。返回 (通过, payload, 原因)。"""
    pub = public_key()
    # RSA 模数是 2048 位大整数，不能用给状态字段用的 as_int（有 2^63 上限）
    try:
        if int(pub.get("n", 0)) <= 0:
            return False, None, "客户端公钥尚未初始化"
    except (TypeError, ValueError, OverflowError):
        return False, None, "客户端公钥格式错误"
    ok, payload, reason = rsa_sign.verify_token(token, pub)
    if not ok:
        return False, None, reason
    if payload.get("v") != LICENSE_VERSION or payload.get("app") != APP_NAME:
        return False, None, "许可格式或产品标识不兼容"
    if payload.get("type") not in VALID_TYPES:
        return False, None, "许可类型不支持（撤销令不能当许可证用）"
    schema_error = _payload_error(payload)
    if schema_error:
        return False, None, schema_error

    score = fingerprint.match_score(fingerprint.components(), payload["fp"])
    # 客户端强制下限，不信任 payload 里的 min_match（防绕过机器绑定）
    required = max(2, min(3, int(payload["min_match"])))
    if score < required:
        return False, None, "机器指纹不匹配，本许可证不在当前电脑签发"

    if state is None:
        state = state_store.load_state()
    if now is None:
        now = clock.trusted_now()
    # 到期/回拨判据用「历史最晚可信时间」做下界：把 now 调回过去、或让
    # last_seen 在停用期间滞后，都不能把已经走过的日历时间抹掉。
    effective_now = max(now, _high_water(state))

    if state:
        if state.get("revoked") and state.get("order") == payload.get("order"):
            return False, None, "许可已作废"
        if state_store.as_int(state.get("max_seq", 0)) > int(payload.get("seq", 0)):
            return False, None, "许可版本过旧，请导入最新签发的许可证"
        activated_at = state_store.as_int(state.get("activated_at", 0), default=0)
        if activated_at <= 0:
            return False, None, "授权状态损坏，请联系作者重签"
        if (payload["type"] == "timed" and
                state_store.as_int(state.get("last_seen", 0)) - effective_now >
                clock.CLOCK_SKEW_TOLERANCE):
            # 始终拒绝；「高水位曾被调快的本地时钟污染」由 check_license()
            # 在确认 NTP 多源共识可信后下调 last_seen 并重验，不在这里放行，
            # 否则直接调用 validate() 的路径会失去回拨检测。
            return False, None, "系统时间异常回拨，请校准时间或联网后重试"
    else:
        # iat 由作者端可信时钟签发，是唯一无法被客户篡改的时间下界。
        # 没有它，客户删掉状态后把时间调回签发之前，就能让已经过期的
        # activate_before 重新通过，凭一张转发的许可再激活一次。
        if now + MIN_IAT_SKEW < int(payload["iat"]):
            return False, None, "系统时间早于许可证签发时间，请校准时间或联网后重试"
        if now > int(payload.get("activate_before", 0)):
            return False, None, "已超过激活窗口，不可再激活，请联系作者重签"
        activated_at = now

    if payload["type"] == "timed":
        duration = _effective_duration(payload, state)
        if effective_now > activated_at + duration:
            return False, None, "许可已到期"
        # 冻结系统时钟会让日历时间停住；心跳用 time.monotonic() 累计真实
        # 运行时长，作为第二道到期/回拨判据，不能被日历时间绕过。
        used = state_store.as_int(state.get("used_seconds", 0)) if state else 0
        if used >= duration:
            return False, None, "许可已到期（累计运行时长）"
        calendar_elapsed = max(0, effective_now - activated_at)
        if used > calendar_elapsed + USAGE_SKEW_TOLERANCE:
            return False, None, "系统时间异常回拨，请校准时间后重试"
        # now 落后于历史最晚可信时间：可能是回拨，也可能是不可信的本地
        # 时钟兜底。直接拒绝，避免「间歇使用 + 回拨」把日历时间洗回去。
        high_water = _high_water(state)
        if high_water > 0 and now < high_water - clock.CLOCK_SKEW_TOLERANCE:
            return False, None, "系统时间异常回拨，请校准时间或联网后重试"
    return True, payload, "OK"


def _new_state(payload: dict, token: str, now: int, bonus_seconds: int = 0) -> dict:
    return {
        "v": 1,
        "order": payload["order"],
        "max_seq": int(payload["seq"]),
        "activated_at": now,
        "last_seen": now,
        "high_water": now,
        "used_seconds": 0,
        "bonus_seconds": max(0, int(bonus_seconds)),
        "revoked": False,
        "revoked_at": 0,
        "challenge": "",
        "token": token,
    }


def activate(token: str, now: int | None = None) -> tuple[bool, dict | None, str]:
    """校验并写入状态。

    限时客户机：
    - 同一激活码重复导入：拒绝（防止同一码反复叠加）。
    - 仍有效时导入不同新码：剩余时长 + 新码时长（写入 bonus_seconds）。
    - 已到期或已撤销后导入新码：仅按新码时长重新计时。
    - 更低 seq：仍拒绝（防回放旧码）。

    ``now`` 可注入计时；真实客户机首次激活仍要求 NTP 可信时间。
    """
    if now is None:
        if state_store.load_state() is None and not clock.activation_time_is_trusted():
            return False, None, ("首次激活需要可信网络时间与本地时间一致，"
                                 "请校准时钟后重试。")
        now = clock.trusted_now()

    outcome = {}

    def _apply(state):
        pub = public_key()
        ok_pre, payload_pre, reason_pre = rsa_sign.verify_token(token, pub)
        if not ok_pre or not isinstance(payload_pre, dict):
            outcome.update(ok=False, payload=None, reason=reason_pre)
            return None

        validate_state = state
        rebind = False
        stack_bonus = 0
        if state:
            if token == (state.get("token") or ""):
                outcome.update(
                    ok=False,
                    payload=None,
                    reason="该激活码已导入，请勿重复使用",
                )
                return None
            max_seq = state_store.as_int(state.get("max_seq", 0))
            seq = int(payload_pre.get("seq", 0))
            if max_seq > seq:
                outcome.update(ok=False, payload=None,
                               reason="更旧版本已作废，请导入最新签发的激活码")
                return None
            newer = seq > max_seq
            revoked = bool(state.get("revoked"))
            remaining = 0 if revoked else _remaining_seconds(state, now)
            if remaining > 0 and payload_pre.get("type") == "timed":
                stack_bonus = remaining
            # 不同 token（含更高 seq 续期）一律按新码校验并重绑
            validate_state = {}
            rebind = True  # {} = 无本地状态；None 会再 load_state

        ok, payload, reason = validate(token, state=validate_state, now=now)
        outcome.update(ok=ok, payload=payload, reason=reason)
        if not ok:
            return None
        if state is None:
            return _new_state(payload, token, now, bonus_seconds=0)
        new_state = dict(state)
        max_seq = state_store.as_int(state.get("max_seq", 0))
        newer = int(payload["seq"]) > max_seq
        new_state["max_seq"] = max(max_seq, int(payload["seq"]))
        if newer or rebind:
            new_state["activated_at"] = now
            new_state["used_seconds"] = 0
            new_state["bonus_seconds"] = (
                stack_bonus if payload.get("type") == "timed" else 0)
            new_state["revoked"] = False
            new_state["revoked_at"] = 0
            new_state["challenge"] = ""
        new_state["order"] = payload["order"]
        new_state["token"] = token
        new_state["last_seen"] = max(
            state_store.as_int(state.get("last_seen", 0)), now)
        new_state["high_water"] = max(_high_water(state), now)
        return new_state

    state_store.update_state(_apply)
    return outcome["ok"], outcome["payload"], outcome["reason"]





def apply_revocation(token: str, now: int | None = None) -> tuple[bool, dict | None, str]:
    """导入撤销令；返回退款回执（含可复算的回执码）。幂等：重复导入不再写盘。"""
    pub = public_key()
    ok, payload, reason = rsa_sign.verify_token(token, pub)
    if not ok:
        return False, None, reason
    if payload.get("v") != LICENSE_VERSION or payload.get("app") != APP_NAME:
        return False, None, "撤销令格式或产品标识不兼容"
    if payload.get("type") != "revoke":
        return False, None, "这不是撤销令"
    schema_error = _revocation_error(payload)
    if schema_error:
        return False, None, schema_error
    if now is None:
        now = clock.trusted_now()

    outcome = {}

    def _apply(state):
        target = (payload.get("target_order") or "").strip()
        machine = fingerprint.machine_code()
        # 取消授权码对的是机器码。旧码仍可能把订单号写在 target_order 里。
        if not state or target not in (state.get("order"), machine):
            outcome.update(ok=False, why="本机没有对应的许可，取消授权码不适用")
            return None
        state = dict(state)
        changed = False
        if not state.get("revoked"):
            state["revoked"] = True
            state["revoked_at"] = now
            state["challenge"] = payload["challenge"]
            changed = True
        elif state.get("challenge") != payload["challenge"]:
            # 补发的撤销令：保留首次停用时间，只更新回执 challenge
            state["challenge"] = payload["challenge"]
            changed = True
        if changed:
            state["last_seen"] = max(
                state_store.as_int(state.get("last_seen", 0)), now)
        outcome.update(ok=True, state=state)
        return state if changed else None

    state_store.update_state(_apply)
    if not outcome.get("ok"):
        return False, None, outcome.get("why", "撤销令不适用")

    state = outcome["state"]
    revoked_at = state_store.as_int(state["revoked_at"])
    machine = fingerprint.machine_code()
    receipt = {
        "order": state["order"],
        "machine_code": machine,
        "revoked_at": revoked_at,
        "revoked_at_utc": clock.format_utc(revoked_at),
        "code": receipt_code(state.get("challenge", ""), machine, revoked_at),
        "reason": payload.get("reason", ""),
    }
    return True, receipt, "OK"


def scan_revocations() -> int:
    """扫描安装目录 / 状态目录里的撤销令；返回成功应用的条数。"""
    applied = 0
    for folder in revocation_dirs():
        if not os.path.isdir(folder):
            continue
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith((".lic", ".revoke", ".txt")):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, "r", encoding="utf-8-sig") as fh:
                    text = fh.read().lstrip("\ufeff")
                if apply_revocation(text)[0]:
                    applied += 1
            except (OSError, UnicodeError, ValueError):
                continue
    return applied


def check_license(strict_time: bool = False) -> tuple[bool, str]:
    """启动时校验（不弹界面），并刷新 last_seen / high_water。

    ``strict_time=True`` 供 launcher 启动入口使用：``timed`` 许可必须本轮
    拿到「NTP 共识 + 本地时钟方向一致」的可信时间，否则拒绝启动。看门狗
    每 10 分钟的复查传 False，靠单调 high_water 保证期间回拨也解不开到期。

    高水位只在交叉校验通过时写：NTP 全断且本地时钟被调快 40 天时，
    绝不能把污染值持久化，否则诚实 NTP 恢复后客户会被永久锁死。
    """
    scan_revocations()
    outcome = {"result": (False, "尚未激活")}

    def _check(state):
        if not state or not state.get("token"):
            outcome["result"] = (False, "尚未激活")
            return None
        last_seen = state_store.as_int(state.get("last_seen", 0))
        high_water = _high_water(state)
        # 时间锚给 NTP 做「不得倒退」的判据，绝不用于抬高 trusted_now()
        clock.set_time_anchor(max(last_seen, high_water))
        if state.get("revoked"):
            outcome["result"] = (False, "许可已作废")
            return None

        cross_checked = clock.time_is_cross_checked()
        ok, payload, reason = validate(state["token"], state=state)
        working = dict(state)

        if not ok and "回拨" in reason and cross_checked:
            # 诚实 NTP 认为时间更早：修正被污染的高水位后重验，避免误伤。
            # **重验失败绝不写盘**：否则每次投毒都能合法擦除一次高水位。
            now = clock.trusted_now()
            candidate = dict(working)
            candidate["last_seen"] = min(last_seen, now)
            candidate["high_water"] = max(high_water, now)
            ok2, payload2, reason2 = validate(state["token"], state=candidate)
            if not ok2:
                outcome["result"] = (ok2, reason2)
                return None
            working, ok, payload, reason = candidate, ok2, payload2, reason2

        if (ok and strict_time and payload and
                payload.get("type") == "timed" and not cross_checked):
            outcome["result"] = (False, "限时版需要联网校时：请检查网络时间（NTP）并校准系统时间后重启")
            return None

        if ok and cross_checked:
            now = clock.trusted_now()
            working["last_seen"] = max(
                state_store.as_int(working.get("last_seen", 0)), now)
            working["high_water"] = max(high_water, now)
            outcome["result"] = (ok, reason)
            return working

        outcome["result"] = (ok, reason)
        return None

    state_store.update_state(_check)
    return outcome["result"]


def expiry_text() -> str:
    """界面用的授权截止时间。本地时区，失败时返回短句，不抛异常。"""
    state = state_store.load_state()
    if not state:
        return "未授权"
    if state.get("revoked"):
        return "已作废"
    token = state.get("token") or ""
    try:
        ok, payload, _ = rsa_sign.verify_token(token, public_key())
    except Exception:
        return "授权无效"
    if not ok or not isinstance(payload, dict):
        return "授权无效"
    if payload.get("type") == "perpetual":
        return "永久授权"
    try:
        activated = state_store.as_int(state.get("activated_at", 0))
        duration = _effective_duration(payload, state)
    except (TypeError, ValueError):
        return "授权无效"
    if activated <= 0 or duration <= 0:
        return "授权无效"
    deadline = time.strftime("%Y-%m-%d %H:%M", time.localtime(activated + duration))
    return "到期 %s" % deadline


def license_summary() -> dict | None:
    state = state_store.load_state()
    if not state:
        return None
    return {
        "order": state.get("order"),
        "max_seq": state_store.as_int(state.get("max_seq", 0)),
        "activated_at": state_store.as_int(state.get("activated_at", 0)),
        "activated_at_utc": clock.format_utc(
            state_store.as_int(state.get("activated_at", 0))),
        "revoked": bool(state.get("revoked")),
        "revoked_at": state_store.as_int(state.get("revoked_at", 0)),
    }


def _heartbeat_tick(elapsed: int) -> None:
    """一次心跳：累计真实运行时长；**只在交叉校验通过时**推进两个高水位。

    无条件写 last_seen 会把「NTP 全断 + 本地时钟被调快」的污染值持久化，
    之后诚实 NTP 反被高水位当倒退拒绝，付费客户被锁死数十天。
    """
    def _apply(state):
        if not state:
            return None
        state = dict(state)
        state["used_seconds"] = (state_store.as_int(
            state.get("used_seconds", 0)) + elapsed)
        if clock.time_is_cross_checked():
            now = clock.trusted_now()
            state["last_seen"] = max(
                state_store.as_int(state.get("last_seen", 0)), now)
            state["high_water"] = max(_high_water(state), now)
        return state

    state_store.update_state(_apply)


def start_heartbeat(interval: int = 60) -> None:
    """后台刷新 last_seen / 累计真实运行时长；进程退出即停。

    ``_START_LOCK`` 把「检查 → clear → 建线程 → 赋值 → start」整段串行化：
    两个线程同时通过 ``is_alive()`` 会各起一个心跳，``used_seconds``
    双倍累计，限时许可提前到期。
    """
    global _HEARTBEAT_THREAD
    with _START_LOCK:
        if _HEARTBEAT_THREAD is not None and _HEARTBEAT_THREAD.is_alive():
            return
        _HEARTBEAT_STOP.clear()

        def _run():
            last = time.monotonic()
            while not _HEARTBEAT_STOP.wait(interval):
                current = time.monotonic()
                elapsed = max(0, int(current - last))
                last = current
                try:
                    _heartbeat_tick(elapsed)
                except Exception:
                    continue

        thread = threading.Thread(target=_run, name="license-heartbeat",
                                  daemon=True)
        _HEARTBEAT_THREAD = thread
        thread.start()


def stop_heartbeat() -> None:
    """设置停止位并等待写线程收尾，避免退出时与状态写入竞态。"""
    global _HEARTBEAT_THREAD
    with _START_LOCK:
        _HEARTBEAT_STOP.set()
        thread = _HEARTBEAT_THREAD
        _HEARTBEAT_THREAD = None
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=3.5)


def ensure_license(parent=None) -> bool:
    """launcher 入口：限时版启动强制校时；校验失败则弹出激活窗口。"""
    ok, reason = check_license(strict_time=True)
    if ok:
        return True
    from licensing import activation_ui
    return activation_ui.show_activation_dialog(parent, reason)
