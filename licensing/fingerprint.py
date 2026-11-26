"""机器指纹：多组件哈希 + 模糊匹配。

换网卡、换硬盘、重装系统、虚拟机都会让单一硬件号漂移，一变就人工重签，
客服成本比破解损失还高。这里把若干组件分别哈希，许可证记录组件列表，
客户端要求匹配到 min_match 项即可。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import time
import uuid

from licensing import rsa_sign

APP_ID = "ShootAtZombies"
# 组件都是 sha256(...)[:16]，机器指纹只允许定长十六进制字符串。
# 客户发来的激活请求是人工粘贴的不可信输入，dict/list 混进来会让
# match_score 的 set(a) 抛 TypeError，必须在解析入口就挡掉。
FP_PATTERN = re.compile(r"[0-9a-fA-F]{8,64}")
_TEST_COMPONENTS = None
_COMPONENTS_CACHE = None


def set_test_components(values: list[str] | None) -> None:
    """TDD/演示用注入；None 恢复真实硬件采集。"""
    global _TEST_COMPONENTS
    _TEST_COMPONENTS = None if values is None else list(values)


def _norm(value: str) -> str:
    return "".join(str(value).strip().lower().split())


def _h(name: str, value: str) -> str:
    return hashlib.sha256(("%s:%s" % (name, _norm(value))).encode("utf-8")).hexdigest()[:16]


def _volume_serial() -> str:
    """C 盘卷序列号，无需管理员权限或外部命令。"""
    if sys.platform != "win32":
        return ""
    try:
        fn = ctypes.windll.kernel32.GetVolumeInformationW
        serial = ctypes.c_ulong(0)
        fn.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                       ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_uint),
                       ctypes.POINTER(ctypes.c_uint), ctypes.c_wchar_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        ok = fn("C:\\", None, 0, ctypes.byref(serial), None, None, None, 0)
        return str(serial.value) if ok else ""
    except Exception:
        return ""


def _machine_guid() -> str:
    """Windows 安装 UUID，重装系统才变。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Cryptography", 0,
                             winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        with key:
            return str(winreg.QueryValueEx(key, "MachineGuid")[0])
    except Exception:
        return ""


def _bios_uuid() -> str:
    """主板 UUID；PowerShell 不可用就跳过这个组件。"""
    if sys.platform != "win32":
        return ""
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance Win32_ComputerSystemProduct).UUID"],
            timeout=4, stderr=subprocess.DEVNULL, creationflags=0x08000000)
        return out.decode("utf-8", "ignore").strip()
    except Exception:
        return ""


def _raw_components() -> list[tuple[str, str]]:
    return [
        ("disk", _volume_serial()),
        ("bios", _bios_uuid()),
        ("guid", _machine_guid()),
        ("mac", "%012x" % uuid.getnode()),
        ("cpu", os.environ.get("PROCESSOR_IDENTIFIER", "") or platform.processor()),
        ("host", os.environ.get("COMPUTERNAME", "")),
    ]


def clear_components_cache() -> None:
    """硬件在进程生命周期内不会变；测试或诊断时可显式重采。"""
    global _COMPONENTS_CACHE
    _COMPONENTS_CACHE = None


def components() -> list[str]:
    global _COMPONENTS_CACHE
    if _TEST_COMPONENTS is not None:
        return list(_TEST_COMPONENTS)
    if _COMPONENTS_CACHE is None:
        # _bios_uuid() 会起 PowerShell（timeout=4）；看门狗每 60 秒调用一次，
        # 不缓存的话最坏每次校验都卡 4 秒。
        result = []
        for name, value in _raw_components():
            if value and _norm(value):
                result.append(_h(name, value))
        _COMPONENTS_CACHE = sorted(result)
    return list(_COMPONENTS_CACHE)


def machine_code(fp: list[str] | None = None) -> str:
    parts = fp if fp is not None else components()
    digest = hashlib.sha256("|".join(sorted(parts)).encode("utf-8")).digest()
    return "SZ-" + base64.b32encode(digest[:10]).decode("ascii").rstrip("=").upper()


def _valid_fp(parts) -> bool:
    if not isinstance(parts, list) or not 2 <= len(parts) <= 16:
        return False
    return all(isinstance(item, str) and FP_PATTERN.fullmatch(item)
               for item in parts)


def match_score(a: list[str], b: list[str]) -> int:
    """只比较字符串组件；畸形元素直接忽略，绝不让外部输入触发 TypeError。"""
    left = {x for x in (a or []) if isinstance(x, str)}
    right = {x for x in (b or []) if isinstance(x, str)}
    return len(left & right)


def min_match_for(n: int) -> int:
    """至少匹配 2 项，最多要求 3 项。"""
    return max(2, min(3, n))


def make_request(fp: list[str] | None = None) -> str:
    parts = fp if fp is not None else components()
    if not _valid_fp(list(parts)):
        raise ValueError("可用硬件组件不足或格式错误，无法生成稳定的机器码")
    data = {
        "v": 1,
        "app": APP_ID,
        "mid": machine_code(parts),
        "fp": sorted(parts),
        "nonce": secrets.token_hex(8),
        "ts": int(time.time()),
    }
    return "%s.%s" % (rsa_sign.REQUEST_PREFIX, rsa_sign.b64e(rsa_sign.canonical(data)))


def parse_request(text: str) -> dict:
    # 客户另存文件时可能带 UTF-8 BOM
    cleaned = "".join(text.split()).lstrip("\ufeff")
    parts = cleaned.split(".")
    if len(parts) != 2 or parts[0] != rsa_sign.REQUEST_PREFIX:
        raise ValueError("激活请求格式不正确（应为 SZREQ1.xxx）")
    data = json.loads(rsa_sign.b64d(parts[1]).decode("utf-8"))
    if not isinstance(data, dict) or data.get("v") != 1 or data.get("app") != APP_ID:
        raise ValueError("激活请求版本或产品标识不匹配")
    if not _valid_fp(data.get("fp")):
        raise ValueError("激活请求的机器指纹缺失或格式错误")
    if not isinstance(data.get("mid"), str) or not data["mid"]:
        raise ValueError("激活请求缺少机器码")
    return data
