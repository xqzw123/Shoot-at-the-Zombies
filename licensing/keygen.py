"""作者端签发器：只在你的电脑上运行，私钥绝不进入安装包。

命令：
    python -m licensing.keygen init                 # 只做一次，生成密钥对（已存在则拒绝）
    python -m licensing.keygen init-state-secret    # 从离线备份恢复/登记状态封装密钥
    python -m licensing.keygen sign --request-file req.txt --type timed --days 7 --order A001
    python -m licensing.keygen renew --order A001 --days 30
    python -m licensing.keygen revoke --order A001 --reason "用户退款"
    python -m licensing.keygen list
    python -m licensing.keygen verify-receipt --order A001 --code XXXX --at 1764000000
    python -m licensing.keygen ui
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys

from licensing import rsa_sign, fingerprint, clock, license_core

DEFAULT_ACTIVATE_DAYS = 7
STATE_SECRET_FILENAME = "state_secret.txt"
# 受版本控制的**指纹锚**（不是密钥本身）。任何构建都必须与它一致，
# 否则升级包会用新密钥，老客户 state.json 解不开且激活窗口早已过期。
STATE_SECRET_FINGERPRINT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state_secret.fingerprint")
_PRIVATE_KEY = None
_PUBLIC_KEY = None
_KEYS_DIR = None
_LEDGER_PATH = None


def set_keys(private: dict | None, public: dict | None) -> None:
    global _PRIVATE_KEY, _PUBLIC_KEY
    _PRIVATE_KEY = private
    _PUBLIC_KEY = public


def set_keys_dir(path: str | None) -> None:
    global _KEYS_DIR
    _KEYS_DIR = path


def set_ledger_path(path: str | None) -> None:
    global _LEDGER_PATH
    _LEDGER_PATH = path


def keys_dir() -> str:
    return _KEYS_DIR or os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys")


def ledger_path() -> str:
    return _LEDGER_PATH or os.path.join(keys_dir(), "orders.jsonl")


def _load_private_from_disk() -> dict:
    path = os.path.join(keys_dir(), "private.json")
    if not os.path.isfile(path):
        raise FileNotFoundError("未找到私钥，请先运行：python -m licensing.keygen init")
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return {"n": int(raw["n"]), "e": int(raw["e"]), "d": int(raw["d"])}


def private_key() -> dict:
    return _PRIVATE_KEY if _PRIVATE_KEY is not None else _load_private_from_disk()


def public_key() -> dict:
    if _PUBLIC_KEY is not None:
        return _PUBLIC_KEY
    return {"n": private_key()["n"], "e": private_key()["e"]}


def state_secret_module_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "state_secret.py")


def state_secret_fingerprint(secret: str) -> str:
    """密钥指纹（可提交、可随包发布），用于跨构建校验密钥没有被换掉。"""
    return hashlib.sha256(bytes.fromhex(secret)).hexdigest()[:32]


def _write_fingerprint(path: str, secret: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(state_secret_fingerprint(secret) + "\n")


def ensure_state_secret(target_dir: str | None = None,
                        module_path: str | None = None,
                        *, fingerprint_path: str | None = None,
                        allow_generate: bool = False) -> str:
    """读取/登记状态封装密钥，并把它注入客户端模块（构建期执行）。

    **绝不静默生成新密钥**：这个密钥必须跨构建保持不变，否则老客户的
    ``state.json`` 解不开，且激活窗口早已过期，连重新激活都做不到。
    ``state_secret.fingerprint`` 是受版本控制的指纹锚；备份或锚缺失时默认
    **一律报错**，要求从离线备份恢复。只有真正创建全新项目时，才由
    ``keygen init --first-run`` 显式传 ``allow_generate=True`` 生成第一份密钥。
    """
    path = os.path.join(target_dir or keys_dir(), STATE_SECRET_FILENAME)
    anchor = fingerprint_path or STATE_SECRET_FINGERPRINT
    has_anchor = os.path.isfile(anchor)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            secret = fh.read().strip()
        if not re.fullmatch(r"[0-9a-fA-F]{64}", secret):
            raise ValueError("状态封装密钥备份格式错误（应为 64 位十六进制）")
        if has_anchor:
            with open(anchor, "r", encoding="utf-8") as fh:
                expected = fh.read().strip()
            if state_secret_fingerprint(secret) != expected:
                raise ValueError(
                    "状态封装密钥与受版本控制的指纹锚不一致：覆盖安装会让全部"
                    "老客户状态解不开。请从离线备份恢复 keys/state_secret.txt")
        else:
            # 首次把既有备份登记为锚（老项目迁移到锚机制），不改变密钥。
            _write_fingerprint(anchor, secret)
    else:
        if has_anchor:
            raise RuntimeError(
                "状态封装密钥备份缺失（%s）。此项目已有发布指纹锚，禁止重新"
                "随机生成；请把离线备份的 state_secret.txt 放回该目录" % path)
        if not allow_generate:
            raise RuntimeError(
                "状态封装密钥与指纹锚都缺失（%s）。已发布项目必须从离线备份"
                "恢复；只有创建全新项目才可用 keygen init --first-run 生成"
                % path)
        secret = secrets.token_hex(32)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(secret)
        _write_fingerprint(anchor, secret)
    target_module = module_path or state_secret_module_path()
    os.makedirs(os.path.dirname(target_module) or ".", exist_ok=True)
    with open(target_module, "w", encoding="utf-8") as fh:
        fh.write('"""构建期注入的状态封装密钥，由 keygen init 生成；已 gitignore，请勿公开。"""\n'
                 'STATE_SECRET = "%s"\n' % secret)
    return secret


def _public_module_text(pub: dict) -> str:
    return ("\"\"\"客户端内置公钥，由 keygen init 生成，请勿手动修改。\"\"\"\n"
            "N = 0x%x\n"
            "E = %d\n"
            "FINGERPRINT = \"%s\"\n"
            % (int(pub["n"]), int(pub["e"]), rsa_sign.public_fingerprint(pub)))


def init_keys(target_dir: str | None = None, keypair: dict | None = None,
              publish: bool = False, force: bool = False,
              allow_new_secret: bool = False) -> dict:
    """生成密钥对。publish=True 时同步覆盖 licensing/public_key.py（仅 CLI init 使用）。

    已有私钥时默认拒绝重建：换 RSA 密钥对会让**所有已发许可**立即失效。
    """
    target = target_dir or keys_dir()
    private_path = os.path.join(target, "private.json")
    if os.path.isfile(private_path) and not force:
        raise RuntimeError(
            "密钥对已存在（%s）。再次生成会作废所有已发许可证；"
            "如确需重建请显式加 --force" % private_path)
    os.makedirs(target, exist_ok=True)
    kp = keypair or rsa_sign.generate_keypair()
    pub = {"n": kp["n"], "e": kp["e"]}
    with open(os.path.join(target, "private.json"), "w", encoding="utf-8") as fh:
        json.dump({"n": str(kp["n"]), "e": kp["e"], "d": str(kp["d"]),
                   "bits": kp["n"].bit_length()}, fh, indent=2)
    text = _public_module_text(pub)
    with open(os.path.join(target, "public_key.py"), "w", encoding="utf-8") as fh:
        fh.write(text)
    if publish:
        module_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "public_key.py")
        with open(module_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    # 状态封装密钥：publish 时注入客户端模块并校验仓库指纹锚；
    # 测试/临时目录用各自的临时锚，绝不碰已发布项目的锚。
    secret_module = (state_secret_module_path() if publish else
                     os.path.join(target, "state_secret.py"))
    secret_anchor = (STATE_SECRET_FINGERPRINT if publish else
                     os.path.join(target, "state_secret.fingerprint"))
    ensure_state_secret(target, secret_module, fingerprint_path=secret_anchor,
                        allow_generate=allow_new_secret if publish else True)
    return pub


def read_ledger() -> list[dict]:
    path = ledger_path()
    if not os.path.isfile(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _append_ledger(record: dict) -> None:
    path = ledger_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _issue_rows(order: str) -> list[dict]:
    return [r for r in read_ledger()
            if r.get("kind") == "issue" and r.get("order") == order]


def issue(order: str, request_text: str, type: str = "perpetual",
          duration_seconds: int = 0, tier: str = "pro",
          activate_days: int = DEFAULT_ACTIVATE_DAYS,
          seq: int | None = None, reason: str = "") -> str:
    """签发许可证；同订单重复签发会报错，续期请用 renew。"""
    if type not in license_core.VALID_TYPES:
        raise ValueError("许可类型只能是 perpetual 或 timed")
    if type == "timed" and int(duration_seconds) <= 0:
        raise ValueError("限时许可必须指定正的时长")
    if int(duration_seconds) < 0:
        raise ValueError("时长不能为负")
    request = fingerprint.parse_request(request_text)
    rows = _issue_rows(order)
    if rows and seq is None:
        raise ValueError("订单 %s 已签发过，请用 renew 续期或换机重签" % order)
    next_seq = int(seq) if seq is not None else 1
    # 客户端首次激活是 fail-closed 的，作者端也必须拿可信时间写 iat /
    # activate_before；用偏移的本地时钟签发会让付费客户无法激活。
    iat = clock.require_trusted_now()
    payload = {
        "v": license_core.LICENSE_VERSION,
        "app": license_core.APP_NAME,
        "type": type,
        "tier": tier,
        "order": order,
        "seq": next_seq,
        "nonce": secrets.token_hex(8),
        "mid": request["mid"],
        "fp": request["fp"],
        "min_match": fingerprint.min_match_for(len(request["fp"])),
        "iat": iat,
        "activate_before": iat + int(activate_days) * 86400,
        "duration_seconds": int(duration_seconds),
        "reason": reason,
        "target_order": "",
        "challenge": "",
    }
    token = rsa_sign.make_token(payload, private_key())
    _append_ledger({
        "kind": "issue",
        "order": order,
        "seq": next_seq,
        "type": type,
        "tier": tier,
        "duration_seconds": int(duration_seconds),
        "iat": iat,
        "mid": request["mid"],
        "request_text": request_text,
        "fp": request["fp"],
        "min_match": payload["min_match"],
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest()[:16],
    })
    return token


def renew(order: str, duration_seconds: int, type: str | None = None) -> str:
    """续期：复用台账里的机器码，客户无需再发一次；seq 递增防止旧许可重置计时。"""
    rows = _issue_rows(order)
    if not rows:
        raise ValueError("订单 %s 不存在，无法续期" % order)
    latest = max(rows, key=lambda r: int(r.get("seq", 0)))
    next_seq = max(int(r.get("seq", 0)) for r in rows) + 1
    return issue(order, latest["request_text"], type or latest["type"],
                 duration_seconds, tier=latest.get("tier", "pro"), seq=next_seq)


def make_revocation(order: str, reason: str = "用户退款") -> tuple[str, str]:
    """生成单张撤销令，返回 (凭证, challenge)。challenge 用于复算退款回执。"""
    challenge = secrets.token_hex(8)
    payload = {
        "v": license_core.LICENSE_VERSION,
        "app": license_core.APP_NAME,
        "type": "revoke",
        "tier": "",
        "order": order,
        "seq": 0,
        "nonce": secrets.token_hex(8),
        "mid": "",
        "fp": [],
        "min_match": 0,
        "iat": clock.require_trusted_now(),
        "activate_before": 0,
        "duration_seconds": 0,
        "reason": reason,
        "target_order": order,
        "challenge": challenge,
    }
    token = rsa_sign.make_token(payload, private_key())
    _append_ledger({"kind": "revoke", "order": order, "challenge": challenge,
                    "iat": payload["iat"], "reason": reason})
    return token, challenge


def verify_receipt(order: str, code: str, revoked_at: int) -> tuple[bool, str]:
    """作者复算客户截图/视频里的回执码。

    重要：回执是流程性证据，不是密码学证明（客户端没有私钥，客户理论上能
    自行算出回执码）。务必配合连续录屏 + 人工审核 + 延时放款。
    """
    rows = read_ledger()
    revokes = [r for r in rows if r.get("kind") == "revoke" and r.get("order") == order]
    issues = [r for r in rows if r.get("kind") == "issue" and r.get("order") == order]
    if not revokes:
        return False, "台账中找不到该订单的撤销令"
    if not issues:
        return False, "台账中找不到该订单的签发记录"
    machine = str(issues[-1].get("mid", ""))
    try:
        at = int(revoked_at)
    except (TypeError, ValueError, OverflowError):
        return False, "回执时间不是有效的 Unix 秒"
    # 同一订单可能补发过多次撤销令；逐一复算，任一匹配即通过。
    for record in revokes:
        expected = license_core.receipt_code(record["challenge"], machine, at)
        if expected != str(code).strip().upper():
            continue
        try:
            issued = int(record.get("iat", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        if at < issued - 86400 or at > issued + 30 * 86400:
            # 补发过多次撤销令时，某一条超窗口不应提前否掉后面有效的一条
            continue
        return True, "回执有效：%s 于 %s 已停用" % (order, clock.format_utc(at))
    return False, "回执码不匹配，截图可能不是本机导入撤销令后生成的"


def _write_out(token: str, out: str | None) -> None:
    text = rsa_sign.split_lines(token)
    if out:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("已写入：%s" % out)
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keygen", description="离线许可证签发器")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="生成密钥对（只做一次；已存在则拒绝）")
    p_init.add_argument("--keys-dir", default=None)
    p_init.add_argument("--force", action="store_true",
                        help="强制重建 RSA 密钥对（会作废所有已发许可证）")
    p_init.add_argument("--first-run", action="store_true",
                        help="全新项目：允许生成第一份状态密钥与指纹锚")

    p_secret = sub.add_parser("init-state-secret",
                              help="从离线备份恢复/登记状态封装密钥（不改动 RSA 密钥对）")
    p_secret.add_argument("--keys-dir", default=None)

    p_sign = sub.add_parser("sign", help="签发许可证")
    p_sign.add_argument("--request", default=None, help="激活请求字符串")
    p_sign.add_argument("--request-file", default=None, help="激活请求文件")
    p_sign.add_argument("--type", choices=["perpetual", "timed"], default="perpetual")
    p_sign.add_argument("--days", type=int, default=0)
    p_sign.add_argument("--hours", type=int, default=0)
    p_sign.add_argument("--order", required=True)
    p_sign.add_argument("--tier", default="pro")
    p_sign.add_argument("--activate-days", type=int, default=DEFAULT_ACTIVATE_DAYS)
    p_sign.add_argument("--out", default=None)

    p_renew = sub.add_parser("renew", help="续期（复用台账机器码）")
    p_renew.add_argument("--order", required=True)
    p_renew.add_argument("--days", type=int, default=0)
    p_renew.add_argument("--hours", type=int, default=0)
    p_renew.add_argument("--out", default=None)

    p_revoke = sub.add_parser("revoke", help="生成撤销令")
    p_revoke.add_argument("--order", required=True)
    p_revoke.add_argument("--reason", default="用户退款")
    p_revoke.add_argument("--out", default=None)

    sub.add_parser("list", help="查看签发台账")

    p_verify = sub.add_parser("verify-receipt", help="校验退款回执")
    p_verify.add_argument("--order", required=True)
    p_verify.add_argument("--code", required=True)
    p_verify.add_argument("--at", type=int, required=True, help="回执上的 Unix 秒")

    sub.add_parser("ui", help="打开轻量图形界面")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "init":
            if args.force:
                print("[警告] --force 会重建 RSA 密钥对，所有已发许可证立即失效！")
            pub = init_keys(args.keys_dir, publish=True, force=args.force,
                            allow_new_secret=args.first_run)
            print("密钥对已生成，公钥指纹：%s" % rsa_sign.public_fingerprint(pub))
            print("请立即离线备份 %s" % os.path.join(args.keys_dir or keys_dir(), "private.json"))
            print("状态封装密钥已注入 licensing/state_secret.py，请一并备份 keys/state_secret.txt")
            print("状态密钥指纹锚：%s（此文件必须提交，跨构建不得变更）"
                  % STATE_SECRET_FINGERPRINT)
        elif args.cmd == "init-state-secret":
            secret = ensure_state_secret(args.keys_dir)
            print("状态封装密钥已就绪，指纹：%s" % state_secret_fingerprint(secret))
            print("已注入 %s" % state_secret_module_path())
            print("请离线备份 %s" % os.path.join(args.keys_dir or keys_dir(),
                                              STATE_SECRET_FILENAME))
        elif args.cmd == "sign":
            request_text = args.request
            if args.request_file:
                with open(args.request_file, "r", encoding="utf-8") as fh:
                    request_text = fh.read()
            if not request_text:
                raise ValueError("需要 --request 或 --request-file")
            duration = args.days * 86400 + args.hours * 3600
            token = issue(args.order, request_text, args.type, duration,
                          tier=args.tier, activate_days=args.activate_days)
            _write_out(token, args.out)
        elif args.cmd == "renew":
            duration = args.days * 86400 + args.hours * 3600
            if duration <= 0:
                raise ValueError("续期需要 --days 或 --hours")
            _write_out(renew(args.order, duration), args.out)
        elif args.cmd == "revoke":
            token, _ = make_revocation(args.order, args.reason)
            _write_out(token, args.out)
        elif args.cmd == "list":
            rows = read_ledger()
            if not rows:
                print("台账为空")
            for row in rows:
                print("%(kind)-7s %(order)-12s seq=%(seq)-3s %(type)-10s %(iat)s" % {
                    "kind": row.get("kind", ""), "order": row.get("order", ""),
                    "seq": row.get("seq", 0), "type": row.get("type", ""),
                    "iat": clock.format_utc(int(row.get("iat", 0)))})
        elif args.cmd == "verify-receipt":
            ok, reason = verify_receipt(args.order, args.code, args.at)
            print(reason)
            return 0 if ok else 1
        elif args.cmd == "ui":
            from licensing import keygen_ui
            keygen_ui.launch()
        else:
            parser.print_help()
        return 0
    except Exception as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
