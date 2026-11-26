"""激活窗口可见性回归测试（TDD 红 → 绿）。

现象：双击发布版 exe 后无任何窗口、进程却一直存活。
根因：launcher.main() 里 root.withdraw() 隐藏主窗口，而激活对话框用
      dialog.transient(parent) 依附到这个隐藏父窗口，Tk 在 Windows 上
      会把 Toplevel 一并置为 withdrawn —— 用户永远看不到激活窗口，
      程序卡在 wait_window 上（--windowed 又吞掉异常，无法察觉）。

修复：激活对话框不 transient 到隐藏父窗口；它本就是独立的模态窗口。
"""
from __future__ import annotations

import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestActivationDialogVisibility(unittest.TestCase):
    def test_dialog_is_not_transient_to_hidden_parent(self):
        """激活对话框不能 transient 到一个隐藏的 parent。

        静态检查源码：show_activation_dialog 内不得出现 transient(parent)。
        """
        import inspect
        from licensing import activation_ui
        src = inspect.getsource(activation_ui.show_activation_dialog)
        self.assertNotIn(
            "dialog.transient(parent)", src,
            "激活对话框 transient 到隐藏主窗口会导致自身不可见（双击没反应）")

    @unittest.skipUnless(os.environ.get("DISPLAY") or os.name == "nt",
                         "需要图形环境")
    def test_dialog_viewable_with_withdrawn_parent(self):
        """真实 Tk：parent 隐藏时，当前实现构造的对话框必须仍可见。"""
        import tkinter as tk
        from licensing import activation_ui
        from unittest import mock

        root = tk.Tk()
        root.withdraw()
        try:
            captured = {}

            original_toplevel = tk.Toplevel

            def spy_toplevel(parent, *a, **kw):
                dlg = original_toplevel(parent, *a, **kw)
                captured["dlg"] = dlg
                return dlg

            # 走到 wait_window 时打断，只验证窗口此时的可见性
            def break_wait(_self):
                root.update_idletasks()
                dlg = captured.get("dlg")
                captured["viewable"] = bool(dlg and dlg.winfo_viewable())
                captured["state"] = dlg.state() if dlg else "none"
                raise _Stop()

            class _Stop(Exception):
                pass

            with mock.patch.object(tk, "Toplevel", spy_toplevel), \
                 mock.patch.object(root, "wait_window", break_wait), \
                 mock.patch.object(activation_ui.license_core, "license_summary",
                                   return_value=None), \
                 mock.patch.object(activation_ui.fingerprint, "machine_code",
                                   return_value="SZ-TEST"), \
                 mock.patch.object(activation_ui.license_core, "build_request",
                                   return_value="SZREQ1.test"):
                try:
                    activation_ui.show_activation_dialog(root, "尚未激活")
                except _Stop:
                    pass
            self.assertTrue(captured.get("viewable"),
                            "激活对话框在隐藏主窗口下不可见：state=%s"
                            % captured.get("state"))
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
