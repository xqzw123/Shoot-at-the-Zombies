"""构建发布包：python packaging/build_release.py --engine pyinstaller

默认（也是唯一实测通过的）交付引擎是 PyInstaller onedir。
Nuitka 档位目前标记为**实验性、未验证**：onefile 的数据目录与
``sys._MEIPASS`` 语义不同，必须在干净虚拟机实测 templates 能加载后才能宣称支持。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import marshal
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_NAME = "ShootAtZombies"
OUT_DIR = os.path.join(ROOT, "dist", APP_NAME)
STATE_SECRET_MODULE = os.path.join(ROOT, "licensing", "state_secret.py")
STATE_SECRET_BACKUP = os.path.join(ROOT, "licensing", "keys", "state_secret.txt")
# 受版本控制的指纹锚：保证换构建机 / 干净 checkout 不会换掉状态封装密钥。
STATE_SECRET_FINGERPRINT = os.path.join(
    ROOT, "licensing", "state_secret.fingerprint")

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX32 = re.compile(r"^[0-9a-fA-F]{32}$")
_SENSITIVE_NAMES = {
    "private.json", "orders.jsonl", "state_secret.txt", "state_secret.py",
    "public_key.py",
    "game_bot.py", "launcher.py", "license_core.py", "keygen.py",
    "keygen_ui.py", "rsa_sign.py", "state_store.py", "clock.py",
    "fingerprint.py", "activation_ui.py",
}
_SENSITIVE_SUFFIXES = (".lic", ".revoke", ".pem", ".key", ".py", ".pyc")
# 项目自有目录（_internal 根、templates、任何新增目录）只允许这些二进制/图片。
# 反向白名单：改名的私钥、台账、日志、客户名单、备份都拦得住，不依赖文件名。
_ALLOWED_PROJECT_SUFFIXES = (
    ".exe", ".dll", ".pyd", ".so", ".zip", ".pdb", ".cat",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp",
)
# 只有这些**具体包目录**才算第三方；share / licenses / tcl 这类通用目录名
# 单独出现时不能当白名单，否则改名后的项目 .py 塞进 share/ 就能绕过扫描。
_THIRD_PARTY_ROOTS = {"cv2", "numpy", "PIL", "win32", "pywin32",
                      "_tcl_data", "_tk_data", "tcl8", "tk8",
                      "numpy.libs", "torch", "scipy", "matplotlib"}
_CONTAINER_DIRS = {"_internal"}
_ALWAYS_SUSPICIOUS_SUFFIXES = (".lic", ".revoke", ".pem", ".key",
                               ".jsonl", ".csv", ".bak", ".log")
_ALLOWED_PROJECT_FILES = {"license.txt", "readme.txt"}


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()


def _state_secret_problems() -> list[str]:
    problems = []
    if not os.path.isfile(STATE_SECRET_MODULE):
        problems.append("缺少 licensing/state_secret.py，请先从离线备份恢复 "
                        "keys/state_secret.txt 再运行："
                        "python -m licensing.keygen init-state-secret")
        return problems
    try:
        match = re.search(r'STATE_SECRET\s*=\s*"([0-9a-fA-F]{64})"',
                          _read_text(STATE_SECRET_MODULE))
        if not match:
            problems.append("licensing/state_secret.py 不是有效的构建期密钥模块")
            return problems
        secret = match.group(1)
    except OSError as exc:
        problems.append("状态封装密钥读取失败：%s" % exc)
        return problems

    if not os.path.isfile(STATE_SECRET_BACKUP):
        problems.append("状态封装密钥的离线备份缺失：%s（跨构建/换构建机必须恢复同一份）"
                        % STATE_SECRET_BACKUP)
    elif not _HEX64.match(_read_text(STATE_SECRET_BACKUP)):
        problems.append("keys/state_secret.txt 格式错误（应为 64 位十六进制）")
    elif _read_text(STATE_SECRET_BACKUP) != secret:
        problems.append("状态封装密钥与 keys/state_secret.txt 不一致，"
                        "覆盖安装后老客户状态会失效")

    if not os.path.isfile(STATE_SECRET_FINGERPRINT):
        problems.append("缺少受版本控制的密钥指纹锚 state_secret.fingerprint，"
                        "请运行：python -m licensing.keygen init-state-secret")
    else:
        anchor = _read_text(STATE_SECRET_FINGERPRINT)
        expected = hashlib.sha256(bytes.fromhex(secret)).hexdigest()[:32]
        if not _HEX32.match(anchor) or anchor != expected:
            problems.append("状态封装密钥与指纹锚不一致：本次构建的密钥与已发布"
                            "版本不同，升级会让全部老客户状态解不开")
    return problems


def _is_third_party_root(name: str) -> bool:
    return (name in _THIRD_PARTY_ROOTS or name.endswith(".dist-info")
            or name.endswith(".libs"))


def _is_third_party_path(rel_base: str) -> bool:
    """第三方判定必须锚定「具体包根目录」，不能匹配任意路径段。

    第六轮工程 M-1：旧实现只要任一段命中 ``share``/``licenses`` 就整体放行，
    ``share/secret.py`` 这类改名的项目源码会被 ``installer.iss`` 递归收进包。
    """
    parts = [p for p in rel_base.split(os.sep) if p and p != "."]
    if not parts:
        return False
    if _is_third_party_root(parts[0]):
        return True
    if parts[0] not in _CONTAINER_DIRS:
        return False
    return any(_is_third_party_root(p) for p in parts[1:])


def dist_problems() -> list[str]:
    """扫描实际发布产物，防止私钥/台账/签发器/项目源码被误打进 setup.exe。

    第五轮工程 H2：仅按「精确文件名 + 少数后缀」匹配时，``private2.json``、
    ``orders_2026.jsonl``、``customers.csv``、``revocation.revoke.bak``
    这类改名文件会被 ``installer.iss`` 的 ``_internal\\*`` 递归收进安装包。
    现在改为「第三方目录白名单放行 + 项目自有目录反向白名单」，默认拒绝。
    """
    if not os.path.isdir(OUT_DIR):
        return []  # 尚未构建，由 --check 文案明确说明「未扫描产物」
    hits = []
    for base, _dirs, files in os.walk(OUT_DIR):
        rel_base = os.path.relpath(base, OUT_DIR)
        third_party = _is_third_party_path(rel_base)
        for name in files:
            rel = os.path.relpath(os.path.join(base, name), OUT_DIR)
            lower = name.lower()
            if lower in _SENSITIVE_NAMES or lower in _ALLOWED_PROJECT_FILES:
                if lower in _SENSITIVE_NAMES:
                    hits.append(rel)
                continue
            if lower.endswith(_ALWAYS_SUSPICIOUS_SUFFIXES):
                hits.append(rel)
                continue
            if third_party:
                # 第三方目录里只额外拦明确的密钥/许可证后缀；其余（.py/.md/
                # .enc/无后缀 tzdata）都是上游自带文件，不算泄露。
                if lower.endswith(_SENSITIVE_SUFFIXES) and not lower.endswith(
                        (".py", ".pyc")):
                    hits.append(rel)
                continue
            if not lower.endswith(_ALLOWED_PROJECT_SUFFIXES):
                hits.append(rel)
    if hits:
        return ["发布产物含敏感文件：%s" % ", ".join(sorted(hits)[:10])]
    return []


def pyz_state_secret_problems() -> list[str]:
    """校验 exe 内嵌 PYZ 里的状态密钥与备份逐字节一致、且不含作者端签发器。

    ``--hidden-import licensing.state_secret`` 一旦丢失，构建仍会成功，
    但升级包会让全部老客户 state.json 解不开——这是覆盖安装的命门，
    必须从人工 pyinstxtractor 提取升级为构建后自动校验。
    """
    exe = os.path.join(OUT_DIR, APP_NAME + ".exe")
    if not os.path.isfile(exe):
        return []
    tmp = os.path.join(tempfile.gettempdir(), "sz_pyz_check_%d.pyz" % os.getpid())
    try:
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader
        arc = CArchiveReader(exe)
        pyz_names = [n for n in arc.toc if n.lower().endswith(".pyz")]
        if not pyz_names:
            return ["发布产物内未找到 PYZ 归档，无法确认状态密钥被打包"]
        data = arc.extract(pyz_names[0])
        with open(tmp, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data[1])
        z = ZlibArchiveReader(tmp)
        if "licensing.state_secret" not in z.toc:
            return ["发布产物缺少 licensing.state_secret，升级后老客户状态会解不开"]
        if any(m.startswith("licensing.keygen") for m in z.toc):
            return ["发布产物含作者端签发器模块"]
        entry = z.extract("licensing.state_secret")
        obj = entry[-1] if isinstance(entry, tuple) else entry
        code = obj if hasattr(obj, "co_consts") else marshal.loads(obj)
        consts = [c for c in code.co_consts if isinstance(c, str) and len(c) == 64]
        expected = _read_text(STATE_SECRET_BACKUP)
        if not consts or consts[0] != expected:
            return ["发布产物内的状态密钥与 keys/state_secret.txt 不一致"]
        return []
    except Exception as exc:
        return ["无法校验发布产物内的状态密钥：%s" % exc]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def anchor_vcs_problems() -> list[str]:
    """**警告**（不阻止构建）：指纹锚必须真正提交到 HEAD，跨构建护栏才成立。

    ``git ls-files`` 会把「仅 git add 暂存」也算进来，而干净 clone 里没有
    暂存文件；必须查 ``git cat-file -e HEAD:<path>``（第六轮工程 M6）。
    """
    try:
        rel = os.path.relpath(STATE_SECRET_FINGERPRINT, ROOT).replace(os.sep, "/")
    except (ValueError, OSError):
        # 单测把锚指向临时目录、与仓库不同盘符时 relpath 会抛错。这不是
        # 「锚未提交」，不该刷误导性 WARN，也不该执行 git cat-file。
        return []
    if rel.startswith(".."):
        return []
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", "HEAD:" + rel], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if result.returncode == 0:
            return []
        return ["状态密钥指纹锚尚未提交到 git HEAD（仅暂存不算）；"
                "交付前请提交 %s，并在干净 clone 上验证 --check" % rel]
    except Exception as exc:
        return ["无法确认指纹锚是否已提交：%s；请人工确认 "
                "licensing/state_secret.fingerprint 在 HEAD 中" % (exc,)]


def preflight(engine: str, require_tools: bool = True) -> list[str]:
    problems = []
    if not os.path.isdir(os.path.join(ROOT, "templates")):
        problems.append("templates/ 不存在")
    if not os.path.isfile(os.path.join(ROOT, "launcher.py")):
        problems.append("发布入口 launcher.py 不存在（禁止用 game_bot.py 打包）")
    private = os.path.join(ROOT, "licensing", "keys", "private.json")
    if not os.path.isfile(private):
        problems.append("未找到私钥，请先运行：python -m licensing.keygen init")
    sys.path.insert(0, ROOT)
    try:
        from licensing import public_key, rsa_sign
        pub = {"n": int(public_key.N), "e": int(public_key.E)}
        if pub["n"] <= 0:
            problems.append("licensing/public_key.py 还是占位值，请先运行 keygen init")
        elif rsa_sign.public_fingerprint(pub) != str(public_key.FINGERPRINT):
            problems.append("licensing/public_key.py 的 FINGERPRINT 与 N 不匹配，"
                            "请重新运行 keygen init 或修正指纹")
    except Exception as exc:
        problems.append("公钥模块读取失败：%s" % exc)
    problems.extend(_state_secret_problems())
    problems.extend(dist_problems())
    problems.extend(pyz_state_secret_problems())
    for warn in anchor_vcs_problems():
        print("[WARN] " + warn)
    if require_tools:
        if engine == "pyinstaller" and importlib.util.find_spec("PyInstaller") is None:
            problems.append("未安装 PyInstaller：python -m pip install pyinstaller")
        if engine == "nuitka" and importlib.util.find_spec("nuitka") is None:
            problems.append("未安装 Nuitka：python -m pip install nuitka（另需 MSVC/MinGW C 编译器）")
    return problems


def build_pyinstaller() -> None:
    # 清空旧的发布目录，避免上次构建的 DLL / 调试残留被 installer 递归收进去。
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--name", APP_NAME,
        # 源路径必须绝对：--specpath 在别处时，PyInstaller 会相对 spec 目录解析相对路径
        "--add-data", os.path.join(ROOT, "templates") + os.pathsep + "templates",
        "--hidden-import", "win32timezone",
        # 构建期生成的状态密钥模块：显式声明，避免静态分析漏收导致发布包
        # 解不开老客户的 state.json（覆盖安装后被迫重新授权）
        "--hidden-import", "licensing.state_secret",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build", "pyinstaller"),
        "--specpath", os.path.join(ROOT, "build", "spec"),
        "launcher.py",
    ]
    print("执行：", " ".join(cmd))
    subprocess.check_call(cmd, cwd=ROOT)
    leaks = dist_problems() + pyz_state_secret_problems()
    if leaks:
        raise RuntimeError("构建产物含敏感文件，禁止打包：\n  " + "\n  ".join(leaks))
    print("\n完成：%s" % os.path.join(OUT_DIR, APP_NAME + ".exe"))
    print("下一步：用 packaging/installer.iss 制作 setup.exe，并在干净虚拟机验收。")


def build_nuitka() -> None:
    """实验性档位：尚未在干净机验证 templates 加载，暂不要用于正式交付。"""
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone", "--onefile", "--windows-disable-console",
        "--enable-plugin=tk-inter",
        "--include-data-dir=templates=templates",
        # 不要整包 include licensing：那会把作者端 keygen/keygen_ui 一起
        # 编进客户端。静态分析会跟踪 launcher 的 import，只需显式补上
        # 构建期生成的 state_secret 模块。
        "--include-module=licensing.state_secret",
        "--output-filename=" + APP_NAME + ".exe",
        # 与 installer.iss 的 Source 对齐
        "--output-dir=" + os.path.join(ROOT, "dist", "ShootAtZombies"),
        "launcher.py",
    ]
    print("执行：", " ".join(cmd))
    print("[实验性] Nuitka onefile 的数据目录与 sys._MEIPASS 语义不同，"
          "发布前必须在干净虚拟机实测 templates 能否加载。")
    subprocess.check_call(cmd, cwd=ROOT)
    leaks = dist_problems()
    if leaks:
        raise RuntimeError("Nuitka 构建产物含敏感文件，禁止打包：\n  "
                           + "\n  ".join(leaks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建发布包")
    parser.add_argument("--engine", choices=["pyinstaller", "nuitka"], default="pyinstaller",
                        help="pyinstaller 为默认且已验证；nuitka 为实验性档位")
    parser.add_argument("--check", action="store_true", help="只做发布前检查")
    args = parser.parse_args(argv)

    problems = preflight(args.engine, require_tools=True)
    if problems:
        print("发布前检查未通过：")
        for item in problems:
            print("  - " + item)
        return 1
    built = os.path.isfile(os.path.join(OUT_DIR, APP_NAME + ".exe"))
    print("发布前检查通过（templates / 发布入口 / 私钥 / 公钥指纹 / "
          "状态密钥与指纹锚 / %s / 当前引擎可用性；未执行构建）"
          % ("产物文件与 PYZ 状态密钥扫描" if built
             else "产物尚未构建，未扫描产物"))
    if args.check:
        return 0
    if args.engine == "pyinstaller":
        build_pyinstaller()
    else:
        build_nuitka()
    return 0


if __name__ == "__main__":
    sys.exit(main())
