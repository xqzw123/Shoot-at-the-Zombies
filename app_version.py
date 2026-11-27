"""应用版本：单一源 VERSION，供 GUI 标题与安装包共用。"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PRODUCT_NAME = "向僵尸开炮助手"
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_ISS_VERSION = re.compile(
    r'(#define\s+MyAppVersion\s+")([^"]+)(")',
)


def _candidate_dirs() -> list[str]:
    dirs: list[str] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)
    if getattr(sys, "frozen", False) or getattr(sys, "__compiled__", None):
        dirs.append(os.path.dirname(os.path.abspath(sys.executable)))
        if sys.argv:
            dirs.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    dirs.append(ROOT)
    # 去重且保序
    seen = set()
    out = []
    for d in dirs:
        if d and d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


def version_file_path() -> str | None:
    for d in _candidate_dirs():
        path = os.path.join(d, "VERSION")
        if os.path.isfile(path):
            return path
    return None


def read_version(default: str = "0.0.0-dev") -> str:
    path = version_file_path()
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return default
    return text or default


def window_title(version: str | None = None) -> str:
    ver = version if version is not None else read_version()
    return "%s v%s" % (PRODUCT_NAME, ver)


def bump_patch(version: str) -> str:
    m = _SEMVER.match(version.strip())
    if not m:
        raise ValueError("版本号须为 x.y.z，收到: %r" % version)
    major, minor, patch = (int(x) for x in m.groups())
    return "%d.%d.%d" % (major, minor, patch + 1)


def write_version(version: str, path: str | None = None) -> str:
    m = _SEMVER.match(version.strip())
    if not m:
        raise ValueError("版本号须为 x.y.z，收到: %r" % version)
    target = path or os.path.join(ROOT, "VERSION")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(version.strip() + "\n")
    return target


def sync_installer_iss(version: str, iss_path: str | None = None) -> None:
    """把 packaging/installer.iss 的 MyAppVersion 改成与 VERSION 一致。"""
    path = iss_path or os.path.join(ROOT, "packaging", "installer.iss")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    new_text, n = _ISS_VERSION.subn(r"\g<1>%s\g<3>" % version.strip(), text, count=1)
    if n != 1:
        raise RuntimeError("installer.iss 未找到唯一的 #define MyAppVersion")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)


def release_bump_patch() -> str:
    """发版：patch +1，写 VERSION 并同步 iss。返回新版本。"""
    old = read_version()
    if not _SEMVER.match(old):
        raise ValueError("当前 VERSION 不是 x.y.z: %r" % old)
    new = bump_patch(old)
    write_version(new)
    sync_installer_iss(new)
    return new
