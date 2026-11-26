"""发布包入口：零侵入接入 game_bot.py。

与主程序之间只有三条契约（见 docs/授权与打包方案.md）：
  1. 打包入口用本文件，而不是 game_bot.py；
  2. frozen 时把工作目录切到 %APPDATA%\\ShootAtZombies，
     解决 config.json 与 screenshots 相对路径写不了；
  3. 以 templates/ 是否真实存在为判据补齐 sys._MEIPASS，保证模板加载。

开发模式（python launcher.py）不切工作目录，方便调试。
"""
from __future__ import annotations

import os
import sys

from licensing import clock, license_core, state_store

# 时间校准类失败只提示、不累计到强制退出：CMOS 没电 / 系统时间被误设的
# 付费客户应该能继续用，而不是 3 分钟后被踢出程序。
_TIME_CALIBRATION_REASONS = ("回拨", "校准", "联网")
MAX_CALIBRATION_GRACE_TICKS = 10   # 时间校准类失败最多豁免 10 分钟，不能无限豁免


def _report_startup_error(message: str, show_dialog: bool = True) -> None:
    """公钥自检/资源定位失败时给出可读提示，避免客户只看到进程闪退。"""
    try:
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                            "ShootAtZombies")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "startup_error.log"), "a",
                  encoding="utf-8") as fh:
            fh.write(message + "\n")
    except OSError:
        pass
    if show_dialog:
        try:
            from tkinter import messagebox
            messagebox.showerror("启动失败",
                                message + "\n请联系作者并提供 startup_error.log")
        except Exception:
            pass


def _prepare_paths() -> None:
    compiled = getattr(sys, "__compiled__", None)
    frozen = getattr(sys, "frozen", False) or compiled is not None
    if not frozen:
        return

    # 关键：所有候选路径必须在 chdir 之前用绝对路径算好，否则相对 argv[0] /
    # __file__ 会被解析到 %APPDATA% 下（第三轮工程 review H3）。
    candidates = [
        getattr(sys, "_MEIPASS", ""),
        os.path.dirname(os.path.abspath(sys.argv[0])),
        os.path.dirname(os.path.abspath(__file__)),
    ]
    # Nuitka onefile 会把 include-data-dir 解压到临时目录；它是实验性档位，
    # 这里尽力补齐常见候选，仍需干净虚拟机实测。
    for env_name in ("NUITKA_ONEFILE_TEMP", "_NUITKA_ONEFILE_TEMP"):
        value = os.environ.get(env_name)
        if value:
            candidates.append(value)

    match = next((base for base in candidates
                  if base and os.path.isdir(os.path.join(base, "templates"))), None)

    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    work_dir = os.path.join(appdata, "ShootAtZombies")
    try:
        os.makedirs(work_dir, exist_ok=True)
        os.chdir(work_dir)
    except OSError as exc:
        message = "无法切换工作目录到 %s：%s" % (work_dir, exc)
        _report_startup_error(message)
        raise RuntimeError(message)

    if match:
        sys._MEIPASS = match
        # game_bot.py 只认 sys.frozen；Nuitka 未必设置它，这里对齐（不改主程序）。
        if compiled is not None and not getattr(sys, "frozen", False):
            sys.frozen = True
        return

    message = "模板目录未找到，候选路径：%s" % " | ".join(str(c) for c in candidates)
    _report_startup_error(message)
    raise RuntimeError(message)


def _shutdown(root, gui) -> None:
    """看门狗退出：先让 GameBotGUI 停机器人线程、恢复 stdout，再退主循环。"""
    try:
        cleanup = getattr(gui, "quit_app", None) if gui is not None else None
        if callable(cleanup):
            cleanup()
            return
    except Exception:
        pass
    try:
        root.quit()
    except Exception:
        pass


def _verify_release_secrets() -> bool:
    """启动自检：公钥自洽 + frozen 产物确实带上了状态封装密钥。

    公钥自检在第二轮 review 已补；这里补的是第六轮 M-1：若构建漏掉
    ``--hidden-import licensing.state_secret``，state_store 会静默退回
    DEV-ONLY 派生密钥，老客户 state.json 解不开 → 变成「尚未激活」，
    而激活窗口早已过期 → 无法重新激活，是最严重的覆盖安装事故。
    """
    if not license_core.public_key_is_consistent():
        _report_startup_error("内置公钥校验失败，可能被替换")
        return False
    frozen = (getattr(sys, "frozen", False) or
              getattr(sys, "__compiled__", None) is not None)
    if frozen and not state_store.state_secret_is_release():
        _report_startup_error("发布包缺少状态封装密钥模块，请联系作者重新下载安装包")
        return False
    return True


def _make_watchdog(root, gui):
    """每 60 秒复查授权；连续 3 次**非时间校准类**失败才退出。

    时间校准类失败只豁免 10 分钟（避免「回拨」类 reason 被无限豁免，
    让已到期/被撤销的程序一直跑），且任何异常都不能让看门狗静默死亡。
    ``clock.start_time_refresher()`` 在启动前已同步预热并交给后台线程刷新，
    这里 ``check_license()`` 永远只做内存计算，不会卡住 Tk 主线程。
    """
    failures = {"count": 0, "grace": 0, "stopped": False}

    def _watch():
        # 停机闩：一旦触发退出，任何残留/重复的 after 回调都不得再次执行，
        # 否则会重复调用 GameBotGUI.quit_app()（重复停线程、重复恢复 stdout）。
        if failures["stopped"]:
            return
        try:
            ok, reason = license_core.check_license()
            calibration = any(key in reason
                              for key in _TIME_CALIBRATION_REASONS)
            if ok:
                failures["count"] = 0
                failures["grace"] = 0
            elif calibration and failures["grace"] < MAX_CALIBRATION_GRACE_TICKS:
                failures["grace"] += 1
                failures["count"] = 0
            else:
                failures["count"] += 1
                if failures["count"] >= 3:
                    failures["stopped"] = True
                    _shutdown(root, gui)
                    return
        except Exception as exc:
            # 只写日志、不弹窗：一次偶发异常不应让授权复查永久停摆
            _report_startup_error("授权复查异常：%r" % (exc,), show_dialog=False)
        if not failures["stopped"]:
            root.after(60000, _watch)

    return _watch


def main() -> int:
    _prepare_paths()
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    try:
        if not _verify_release_secrets():
            raise RuntimeError("启动自检失败，详见 startup_error.log")
        # 后台 NTP 刷新：启动同步预热一次，之后主线程不再做网络 I/O
        clock.start_time_refresher()
        if not license_core.ensure_license(parent=root):
            return 0
        license_core.start_heartbeat()
        root.deiconify()
        import game_bot
        gui = game_bot.GameBotGUI(root)

        # 运行中限时到期或撤销令生效会主动退出，不依赖用户重启，
        # 且完全不改 game_bot.py。
        root.after(60000, _make_watchdog(root, gui))
        root.mainloop()
    finally:
        # 先停心跳并等写线程收尾，避免退出时与状态写入竞态
        license_core.stop_heartbeat()
        clock.stop_time_refresher()
        try:
            root.destroy()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
