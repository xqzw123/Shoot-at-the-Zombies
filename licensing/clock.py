"""可信时间：公共 NTP 多源共识 + 状态高水位 + 单调时钟缓存。

设计要点（第三~六轮 review 修正）：

1. **NTP 样本必须秒级一致**：共识判据是「每个样本与中位数偏差 ≤30 秒」，
   旧实现用「极差 ≤1 天」会让控制 2/3 源、偏移 20 小时的投毒成为共识（第六轮 H-2）。
2. **不以本地时钟决定 NTP 是否被采信（过去方向例外）**：网络时间领先本地时钟
   不限（系统时钟慢 40 天的诚实客户仍能拿到真值）；网络时间**落后**本地时钟
   超过 ``MAX_CLOCK_SKEW`` 时不采信——「往回拨」是唯一能延长限时的方向。
   这与「同时控制全部 NTP 且持续调回本地时间」的已接受边界对齐。
3. **锚只用于拒绝远期投毒与识别过去方向的污染，不抬高 now**。小幅倒退是
   唯一能延长限时的方向；多源一致落后于锚时下调锚自愈，避免高水位曾被
   污染的客户永久锁死。下调只影响锚，不影响状态里单调的 ``high_water``。
4. **主线程只在启动预热与首次激活/签发时做一次网络 I/O**：之后后台每
   300 秒刷新；``trusted_now()`` 缓存有效时永远只做内存计算，缓存过期也
   不再每次触发同步 NTP（第四轮 H1）。
5. **首次激活与作者端签发都 fail-closed**：必须本轮 NTP 共识成功且交叉
   校验通过。日常复查默认不 fail-closed，但 ``timed`` 启动走严格校时，
   避免「断 NTP + 回拨本地时间」让已过期许可在间歇使用场景下复活（第六轮 B-1）。
   ``perpetual`` 不做任何时间校验。

游戏必须联网，但不自建服务器，只用公共 NTP。时间统一为 Unix 秒（int）。
"""
from __future__ import annotations

import datetime
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

NTP_HOSTS = ("time.windows.com", "ntp.aliyun.com", "pool.ntp.org")
NTP_TIMEOUT = 1.5
CLOCK_SKEW_TOLERANCE = 600      # 状态回拨超过 10 分钟即拒绝
MAX_NTP_SKEW = 30               # 每个 NTP 样本与中位数的最大偏差（秒）
MAX_FORWARD_SKEW = 30 * 86400   # 相对「已知可信锚」的远期投毒上界
MIN_AGREEING_SAMPLES = 2        # 至少两个可用样本，且**全部**互相认同才算共识
MAX_CLOCK_SKEW = 2 * 86400      # 网络时间落后本地时钟的最大容差（过去方向）
# 兼容旧名：首次激活与日常校时现在共用同一套交叉校验口径
MAX_ACTIVATION_CLOCK_SKEW = MAX_CLOCK_SKEW
NTP_RETRY_BACKOFF = 300         # NTP 失败后的重试退避
REFRESH_INTERVAL = 300          # 后台刷新周期

_TEST_CLOCK = None
_NTP_ENABLED = True
_TIME_ANCHOR = None             # 高水位，仅用于识别倒退/污染的 NTP 样本
_LOCK = threading.RLock()
_START_LOCK = threading.Lock()   # 让 start/stop 与「检查是否已启动」串行
# valid  : 缓存是否建立
# trusted: 缓存里的 unix 是否来自本轮 NTP 共识（而非本地时间兜底）
_CACHE = {"unix": None, "mono": None, "valid": False, "trusted": False}
_NEXT_RETRY = 0.0
_STOP = threading.Event()
_REFRESH_THREAD = None


def set_test_clock(fn) -> None:
    global _TEST_CLOCK
    _TEST_CLOCK = fn


def set_ntp_enabled(enabled: bool) -> None:
    global _NTP_ENABLED
    _NTP_ENABLED = bool(enabled)


def set_time_anchor(ts: int | None) -> None:
    """由 license_core 在读到状态时设置，作为「NTP 不得倒退」的下界。

    注意：这不是 ``trusted_now()`` 返回值的下界。把返回值抬到锚会让
    ``last_seen - now > tolerance`` 永远不成立，回拨检测就废了。
    """
    global _TIME_ANCHOR
    try:
        value = int(ts) if ts is not None else 0
    except (TypeError, ValueError, OverflowError):
        value = 0
    with _LOCK:
        _TIME_ANCHOR = value if value > 0 else None


def reset_cache() -> None:
    """清空缓存锚与重试退避，用于模拟新进程 / 单元测试。"""
    global _NEXT_RETRY
    with _LOCK:
        _CACHE.update({"unix": None, "mono": None,
                       "valid": False, "trusted": False})
        _NEXT_RETRY = 0.0


def parse_ntp_reply(data: bytes) -> int | None:
    """校验 NTP 包基本字段，拒绝无效/未同步的服务器响应。"""
    try:
        if len(data) < 48:
            return None
        li = data[0] >> 6
        mode = data[0] & 0x07
        stratum = data[1]
        if li == 3 or mode not in (4, 5) or stratum == 0 or stratum > 15:
            return None
        transmit = struct.unpack("!12I", data[:48])[10]
        if transmit == 0:
            return None
        return int(transmit - 2208988800)  # 1900 -> 1970
    except Exception:
        return None


def _ntp_seconds(host: str) -> int | None:
    try:
        packet = b"\x1b" + b"\x00" * 47
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(NTP_TIMEOUT)
            sock.sendto(packet, (host, 123))
            reply, _ = sock.recvfrom(1024)
        return parse_ntp_reply(reply)
    except Exception:
        return None


def _query_samples() -> list[int]:
    """并发查询所有 NTP 源，总耗时约等于单个超时，而不是三倍。"""
    samples = []
    try:
        with ThreadPoolExecutor(max_workers=len(NTP_HOSTS)) as pool:
            futures = [pool.submit(_ntp_seconds, host) for host in NTP_HOSTS]
            for future in futures:
                try:
                    value = future.result(timeout=NTP_TIMEOUT + 1.0)
                except Exception:
                    value = None
                if value is not None:
                    samples.append(value)
    except Exception:
        return samples
    return samples


def ntp_consensus() -> int | None:
    """从 NTP 样本里得到可信 Unix 秒；无法取得共识时返回 None。

    基准全部来自「样本之间」或「已验证的可信锚」，**绝不读取 time.time()**。
    """
    samples = _query_samples()
    if len(samples) < MIN_AGREEING_SAMPLES:
        return None
    ordered = sorted(samples)
    # 「全部互相认同」必须是**每个样本都与中位数接近**，而不是「极差 ≤1 天」：
    # 后者会让 [T, T+20h, T+20h] 这类 2/3 源投毒以 20 小时偏移成为共识。
    result = ordered[(len(ordered) - 1) // 2]
    if any(abs(value - result) > MAX_NTP_SKEW for value in ordered):
        return None

    with _LOCK:
        anchor = _TIME_ANCHOR
        cached = _CACHE["unix"] if (_CACHE["valid"] and _CACHE["trusted"]) else None
    baseline = anchor if anchor is not None else cached
    # 一次性远期投毒会把 last_seen 抬到多年后，之后诚实 NTP 反被高水位当
    # 倒退拒绝，客户被永久锁死。上界只相对已知可信锚/缓存判断。
    if baseline is not None and result > baseline + MAX_FORWARD_SKEW:
        return None

    if anchor is not None and result < anchor - CLOCK_SKEW_TOLERANCE:
        # 多源一致且落后于锚：判定为「历史锚曾被调快的本地时钟污染」，
        # 下调锚并采信诚实 NTP。旧实现只在落后 >30 天时自愈，1~30 天污染
        # 会让诚实 NTP 永远进不来，付费客户被锁死数十天。
        # 攻击者控制全部可用 NTP 响应属已接受限制，且 used_seconds 与
        # last_seen 回拨检测仍守住累计运行时长，时间不会被用来延长许可。
        set_time_anchor(result)
        return result
    return result


def _cached_now() -> int:
    with _LOCK:
        unix, mono, valid = _CACHE["unix"], _CACHE["mono"], _CACHE["valid"]
    if not valid or mono is None:
        return int(time.time())
    age = max(0.0, time.monotonic() - mono)
    return int(unix + age)


def time_is_trusted() -> bool:
    """当前缓存是否来自 NTP 共识（注入的测试时钟视为可信）。

    注意：这只回答「来源」，不回答「是否与本地时钟方向一致」。放行写
    ``last_seen`` / 自愈 / 限时启动必须用 ``time_is_cross_checked()``。
    """
    if _TEST_CLOCK is not None:
        return True
    if not _NTP_ENABLED:
        return False
    with _LOCK:
        return bool(_CACHE["valid"] and _CACHE["trusted"])


def _cross_check_ok(unix: int, local: int) -> bool:
    """**非对称**交叉校验：只允许网络时间领先，不允许大幅落后。

    系统时钟慢几天的诚实客户，其本地时间落后 NTP，必须照样采信 NTP；
    反过来 NTP 大幅落后本地时钟，要么是投毒、要么是本机时钟被调快，
    两者都不该作为「可信时间」放行。攻击者若同时把本地时钟也调回过去，
    就落入已接受的「全源 NTP 控制且持续调回本地时间」边界。
    """
    return unix >= local - MAX_CLOCK_SKEW


def cross_check_ok(unix: int, local: int) -> bool:
    """公开口径，供发布前冒烟验证使用（语义见 ``_cross_check_ok``）。"""
    return _cross_check_ok(unix, local)


def time_is_cross_checked() -> bool:
    """缓存时间是否「来自 NTP 共识 **且** 与本地时钟方向一致」。

    ``time_is_trusted()`` 只说明缓存来自某轮共识（投毒共识也算），
    不能单独用来放行写高水位/自愈/限时启动；这里才是安全判定入口。
    """
    if _TEST_CLOCK is not None:
        return True
    if not time_is_trusted():
        return False
    return _cross_check_ok(_cached_now(), int(time.time()))


def activation_time_is_trusted() -> bool:
    """首次激活专用：强制刷新一次，要求 NTP 共识 + 交叉校验都通过。"""
    if _TEST_CLOCK is not None:
        return True
    if not _NTP_ENABLED:
        return False
    refresh_now(force=True)
    return time_is_cross_checked()


def require_trusted_now() -> int:
    """作者端签发专用：拿不到可信时间就拒绝签发，不能静默用本地时钟。

    客户端首次激活是 fail-closed 的；作者端若用偏移的本地时间写 ``iat``
    和 ``activate_before``，客户拿诚实 NTP 激活会被「系统时间早于签发时间」
    拒之门外。两端口径必须一致（第六轮工程 H1）。
    """
    if _TEST_CLOCK is not None:
        return int(_TEST_CLOCK())
    if not _NTP_ENABLED:
        raise RuntimeError("签发时间不可信：NTP 已停用，请联网校时后重试")
    refresh_now(force=True)
    if not time_is_cross_checked():
        raise RuntimeError("签发时间不可信：请联网校时（NTP）并校准系统时间后重试")
    return _cached_now()


def refresh_now(force: bool = False) -> int:
    """同步刷新时间锚；NTP 失败/交叉校验不过时绝不把缓存标为可信。

    ``force=True`` 忽略退避，供首次激活、作者端签发与启动预热使用。
    """
    global _NEXT_RETRY
    with _LOCK:
        if not force and time.monotonic() < _NEXT_RETRY:
            return _cached_now()
    unix = ntp_consensus() if _NTP_ENABLED else None
    local = int(time.time())
    mono = time.monotonic()
    if unix is not None and not _cross_check_ok(unix, local):
        # 网络时间大幅落后本机时钟：宁可标记为不可信并提示校时，也不能
        # 让「过去方向」的投毒成为写入状态高水位的时间源。
        unix = None
    with _LOCK:
        if unix is not None:
            _CACHE.update({"unix": unix, "mono": mono,
                           "valid": True, "trusted": True})
            _NEXT_RETRY = 0.0
        else:
            _NEXT_RETRY = mono + NTP_RETRY_BACKOFF
            if not _CACHE["valid"]:
                # 冷缓存且无可信时间：只能用原始本地时间，由 validate() 的
                # 回拨检测兜底；标记 untrusted 以免污染 last_seen 高水位。
                _CACHE.update({"unix": local, "mono": mono,
                               "valid": True, "trusted": False})
            else:
                # 保留旧锚并把单调基准平移到当前，缓存不会「永久过期」；
                # 刷新交给后台线程，主线程不再重复做网络 I/O。
                current = int(_CACHE["unix"] +
                              max(0.0, mono - _CACHE["mono"]))
                _CACHE.update({"unix": current, "mono": mono,
                               "trusted": False})
    return _cached_now()


def trusted_now() -> int:
    if _TEST_CLOCK is not None:
        return int(_TEST_CLOCK())
    if not _NTP_ENABLED:
        return int(time.time())  # 原始本地时间，让 validate() 能检出回拨
    with _LOCK:
        valid = _CACHE["valid"]
    if not valid:
        return refresh_now(force=True)  # 冷启动只做一次同步取样
    return _cached_now()               # 缓存过期也只是内存计算


def start_time_refresher(interval: int = REFRESH_INTERVAL) -> None:
    """启动前同步预热一次，之后后台刷新。

    主线程只在「启动预热 / 首次激活 / 作者端签发」三处做一次网络 I/O；
    看门狗与心跳永远只读内存缓存或 ``monotonic``。

    ``_START_LOCK`` 把「检查是否已启动 → 预热 → 创建线程」整段串行化，
    避免两个线程同时通过 is_alive() 检查而各起一个刷新线程。
    """
    global _REFRESH_THREAD
    with _START_LOCK:
        with _LOCK:
            if _REFRESH_THREAD is not None and _REFRESH_THREAD.is_alive():
                return
            _STOP.clear()

        refresh_now(force=True)

        def _loop():
            while not _STOP.is_set():
                if _STOP.wait(interval):
                    break
                try:
                    refresh_now()
                except Exception:
                    pass

        with _LOCK:
            _REFRESH_THREAD = threading.Thread(target=_loop, name="license-time",
                                               daemon=True)
            _REFRESH_THREAD.start()


def stop_time_refresher() -> None:
    global _REFRESH_THREAD
    with _START_LOCK:
        _STOP.set()
        with _LOCK:
            thread = _REFRESH_THREAD
            _REFRESH_THREAD = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.5)


def format_utc(ts: int) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC")
