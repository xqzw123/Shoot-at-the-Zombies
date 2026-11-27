"""授权状态：文件 + 注册表双副本，密文封装 + 认证校验。

状态目录固定在 %APPDATA%/jiangshikaipao，不在安装目录里，
因此覆盖安装/卸载重装都不会丢授权。

关于安全性：纯离线、客户端持有全部逻辑时，本地状态无法做到密码学绝对可靠。
封装密钥由 ``keygen init`` 生成，保存在作者端 ``licensing/keys/state_secret.txt``，
构建期注入 ``licensing/state_secret.py``（已 gitignore，不随公开源码分发）。
它必须在**每次构建之间保持不变**，否则老客户的 state.json 会解不开，
「覆盖安装免重新授权」就失效了。状态以密文存储，攻击者必须逆向二进制
才能拿到密钥并伪造状态——把门槛从「改一个 JSON 字段」抬到「逆向 + 提取密钥」。
这是可接受的工程折中，不是不可破解的保证。

源码 checkout 里没有生成模块时，会退化成从公开公钥派生的 DEV-ONLY 密钥；
``build_release.py --check`` 会明确阻止用这种状态发布。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading

from licensing import rsa_sign

# 顶层静态导入，PyInstaller/Nuitka 才能把这个构建期生成的模块收进产物。
# 动态放在函数里的 import 有可能被静态分析漏掉，导致发布包解不开老客户状态。
try:
    from licensing import state_secret as _state_secret_module
except ImportError:  # 干净 checkout 尚未执行 init-state-secret
    _state_secret_module = None

STATE_FILENAME = "state.json"
REG_PATH = r"Software\jiangshikaipao\License"
_OLD_REG_PATH = r"Software\ShootAtZombies\License"
REG_VALUE = "mirror"
_MAX_INT = 2 ** 63 - 1
_INT_STATE_FIELDS = ("max_seq", "activated_at", "last_seen",
                     "used_seconds", "bonus_seconds", "revoked_at", "high_water")

_IO_LOCK = threading.RLock()
_TMP_CLEANED = False
_APP_DIR_OVERRIDE = None
_REGISTRY_ENABLED = True
_LAST_REGISTRY_ERROR = None


def _secret_from_module() -> bytes | None:
    """唯一的密钥解析口径：运行时自检与 _load_secret 共用，避免口径漂移。"""
    try:
        text = _state_secret_module.STATE_SECRET
        if not isinstance(text, str) or len(text) != 64:
            return None
        raw = bytes.fromhex(text)
        return raw if len(raw) == 32 else None
    except (AttributeError, TypeError, ValueError):
        return None


def _load_secret() -> bytes:
    secret = _secret_from_module()
    if secret is not None:
        return secret
    # 仅用于开发/单元测试：从公开数据派生，发布前检查会拒绝这种状态。
    from licensing import public_key
    return hashlib.sha256(
        ("DEV-ONLY-STATE-SECRET|" + str(public_key.N)).encode("ascii")).digest()


def state_secret_is_release() -> bool:
    """给发布前检查用：确认当前用的是构建期注入的随机密钥（非 DEV-ONLY）。"""
    return _secret_from_module() is not None


_STATE_SECRET = _load_secret()


def set_app_dir(path: str | None) -> None:
    global _APP_DIR_OVERRIDE
    _APP_DIR_OVERRIDE = path


def set_registry_enabled(enabled: bool) -> None:
    global _REGISTRY_ENABLED
    _REGISTRY_ENABLED = bool(enabled)


def last_registry_error() -> str | None:
    return _LAST_REGISTRY_ERROR


def as_int(value, default: int = 0) -> int:
    """把所有外部/持久化数字收敛成安全整数；畸形值取默认值而不是崩溃。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if result < 0 or result > _MAX_INT:
        return default
    return result


def app_dir() -> str:
    if _APP_DIR_OVERRIDE:
        base = _APP_DIR_OVERRIDE
    else:
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        base = os.path.join(root, "jiangshikaipao")
        old = os.path.join(root, "ShootAtZombies")
        # 旧目录只搬一次。授权状态不在安装目录里，改名后要还能读到。
        if not os.path.exists(base) and os.path.isdir(old):
            try:
                os.rename(old, base)
            except OSError:
                base = old
        os.makedirs(base, exist_ok=True)
        return base
    os.makedirs(base, exist_ok=True)
    return base


def state_path() -> str:
    return os.path.join(app_dir(), STATE_FILENAME)


def _keystream(nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(_STATE_SECRET,
                            b"ks" + nonce + counter.to_bytes(4, "big"),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _seal(state: dict) -> str:
    raw = rsa_sign.canonical(state)
    nonce = secrets.token_bytes(16)
    cipher = bytes(a ^ b for a, b in zip(raw, _keystream(nonce, len(raw))))
    mac = hmac.new(_STATE_SECRET, b"mac" + nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + mac + cipher).decode("ascii")


def _unseal(blob: str) -> dict | None:
    try:
        data = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        if len(data) < 48:
            return None
        nonce, mac, cipher = data[:16], data[16:48], data[48:]
        expected = hmac.new(_STATE_SECRET, b"mac" + nonce + cipher,
                            hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected):
            return None
        plain = bytes(a ^ b for a, b in zip(cipher, _keystream(nonce, len(cipher))))
        state = json.loads(plain.decode("utf-8"))
        return state if isinstance(state, dict) else None
    except Exception:
        return None


def _valid(state: dict | None) -> dict | None:
    """净化解封后的状态：字段类型/范围一律收敛，避免下游 int() 崩溃。"""
    if not isinstance(state, dict):
        return None
    order = state.get("order")
    if not isinstance(order, str) or not order.strip():
        return None
    cleaned = dict(state)
    for key in _INT_STATE_FIELDS:
        cleaned[key] = as_int(state.get(key, 0))
    cleaned["token"] = state.get("token") if isinstance(state.get("token"), str) else ""
    cleaned["challenge"] = (state.get("challenge")
                            if isinstance(state.get("challenge"), str) else "")
    cleaned["revoked"] = bool(state.get("revoked"))
    cleaned["order"] = order
    return cleaned


def merge_states(states: list[dict | None]) -> dict | None:
    """取更严格的一份；token/order 必须与最大的 max_seq 同源。"""
    valid = [s for s in states if isinstance(s, dict) and s.get("order")]
    if not valid:
        return None
    best = max(valid, key=lambda s: as_int(s.get("max_seq", 0)))
    merged = dict(valid[0])
    for other in valid[1:]:
        a = as_int(merged.get("activated_at", 0))
        b = as_int(other.get("activated_at", 0))
        merged["activated_at"] = min(a, b) or a
        merged["last_seen"] = max(as_int(merged.get("last_seen", 0)),
                                  as_int(other.get("last_seen", 0)))
        # high_water 是「历史最晚可信时间」，只增不减；自愈下调 last_seen
        # 时绝不能把它一起拉低，否则 timed 到期判据会被回拨绕过。
        merged["high_water"] = max(as_int(merged.get("high_water", 0)),
                                   as_int(other.get("high_water", 0)))
        merged["max_seq"] = max(as_int(merged.get("max_seq", 0)),
                                as_int(other.get("max_seq", 0)))
        merged["used_seconds"] = max(as_int(merged.get("used_seconds", 0)),
                                     as_int(other.get("used_seconds", 0)))
        merged["revoked"] = bool(merged.get("revoked")) or bool(other.get("revoked"))
        merged["revoked_at"] = max(as_int(merged.get("revoked_at", 0)),
                                   as_int(other.get("revoked_at", 0)))
        if other.get("challenge"):
            merged["challenge"] = other["challenge"]
    merged["token"] = best.get("token") or merged.get("token", "")
    merged["order"] = best.get("order") or merged.get("order", "")
    merged["bonus_seconds"] = as_int(best.get("bonus_seconds", 0))
    return merged


def _temp_path(path: str) -> str:
    """唯一临时文件名：心跳线程与看门狗同时写时不能互相踩踏。"""
    return os.path.join(os.path.dirname(path),
                        ".state-%s.tmp" % secrets.token_hex(8))


def _write_file_blob(blob: str) -> None:
    global _TMP_CLEANED
    path = state_path()
    # 进程被强杀时可能留下上次未完成的 .state-*.tmp。只在**本进程第一次写**
    # 且持有 _IO_LOCK 时清理，避免删掉其它线程正在使用的临时文件。
    if not _TMP_CLEANED:
        _TMP_CLEANED = True
        try:
            folder = os.path.dirname(path)
            for name in os.listdir(folder):
                if name.startswith(".state-") and name.endswith(".tmp"):
                    try:
                        os.remove(os.path.join(folder, name))
                    except OSError:
                        pass
        except OSError:
            pass
    tmp = _temp_path(path)
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "sealed": blob}, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _read_file() -> dict | None:
    path = state_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _unseal(json.load(fh).get("sealed", ""))
    except Exception:
        return None


def _write_registry_blob(blob: str) -> None:
    global _LAST_REGISTRY_ERROR
    if not _REGISTRY_ENABLED or sys.platform != "win32":
        return
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REG_PATH, 0,
                               winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, REG_VALUE, 0, winreg.REG_SZ,
                              json.dumps({"v": 1, "sealed": blob}))
        _LAST_REGISTRY_ERROR = None
    except Exception as exc:
        _LAST_REGISTRY_ERROR = str(exc)


def _query_registry_raw(path: str):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
        return winreg.QueryValueEx(key, REG_VALUE)[0]


def _read_registry() -> dict | None:
    if not _REGISTRY_ENABLED or sys.platform != "win32":
        return None
    try:
        import winreg
        try:
            raw = _query_registry_raw(REG_PATH)
        except OSError:
            raw = None
        if raw is None:
            try:
                raw = _query_registry_raw(_OLD_REG_PATH)
            except OSError:
                raw = None
            if raw is not None:
                # 新键写成功后再删旧键，避免搬走失败时把唯一副本删掉。
                _write_registry_blob(json.loads(raw).get("sealed", ""))
                if _LAST_REGISTRY_ERROR is None:
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _OLD_REG_PATH)
                    except OSError:
                        pass
        if not raw:
            return None
        return _unseal(json.loads(raw).get("sealed", ""))
    except Exception:
        return None


def _save_state_unlocked(state: dict) -> None:
    stored = dict(state)
    stored["v"] = 1
    blob = _seal(stored)
    _write_file_blob(blob)
    _write_registry_blob(blob)


def save_state(state: dict) -> None:
    with _IO_LOCK:
        _save_state_unlocked(state)


_DIVERGE_FIELDS = ("order", "max_seq", "activated_at", "last_seen",
                   "used_seconds", "bonus_seconds", "revoked", "revoked_at", "token", "challenge",
                   "high_water")


def _read_pair() -> tuple[dict | None, dict | None]:
    return _valid(_read_file()), _valid(_read_registry())


def _load_state_unlocked() -> dict | None:
    """只读合并，不回写：供 update_state() 在 fn 放弃写入时保持「不写盘」。"""
    file_state, reg_state = _read_pair()
    return merge_states([file_state, reg_state])


def load_state() -> dict | None:
    with _IO_LOCK:
        file_state, reg_state = _read_pair()
        merged = merge_states([file_state, reg_state])
        # 两份副本 diverge 时用更严格的结果回写；比对全部关键字段，
        # 否则只改了 used_seconds / revoked 的分叉会长期不收敛，回退到
        # 旧副本时会丢失累计运行时长（削弱冻结时钟的第二道判据）。
        if (merged and file_state and reg_state and
                any(file_state.get(k) != reg_state.get(k)
                    for k in _DIVERGE_FIELDS)):
            _save_state_unlocked(merged)
        return merged


def update_state(fn) -> dict | None:
    """原子读-改-写：整个 load → fn(state) → save 序列都在 _IO_LOCK 内。

    看门狗 ``check_license()`` 与心跳线程都会改 ``last_seen`` / ``used_seconds``，
    若各自持锁只包含单次 load/save，交错执行会丢失更新（第四轮 H2）。
    ``fn`` 返回 None 表示放弃写入。
    """
    with _IO_LOCK:
        state = _load_state_unlocked()
        result = fn(state)
        if result is None:
            return state
        _save_state_unlocked(result)
        return result


def clear_state() -> None:
    with _IO_LOCK:
        try:
            os.remove(state_path())
        except OSError:
            pass
        if _REGISTRY_ENABLED and sys.platform == "win32":
            try:
                import winreg
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_PATH)
            except Exception:
                pass
