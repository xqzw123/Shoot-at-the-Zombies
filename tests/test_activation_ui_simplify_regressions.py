"""激活窗口简化回归测试（TDD 红 → 绿）。

客户反馈：窗口同时要「复制机器码」和「复制激活请求」，看起来重复。
数据事实：激活请求（SZREQ1.*）里已含机器码 mid 字段，签发器只吃请求，
机器码是冗余展示。按用户决定：删掉「复制机器码」按钮，机器码改为
纯文本并标注「无需发送」，只保留激活请求的复制/导出。
"""
from __future__ import annotations

import inspect
import os
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _all_button_texts(widget):
    texts = []
    for child in widget.winfo_children():
        try:
            if child.winfo_class() in ("TButton", "Button"):
                texts.append(str(child.cget("text")))
        except Exception:
            pass
        texts.extend(_all_button_texts(child))
    return texts


class TestActivationUISimplified(unittest.TestCase):
    def test_no_copy_machine_button_in_source(self):
        """源码里不再有「复制机器码」按钮。"""
        from licensing import activation_ui
        src = inspect.getsource(activation_ui.show_activation_dialog)
        self.assertNotIn('复制机器码', src,
                         "仍存在多余的「复制机器码」按钮")

    def test_machine_label_says_no_need_to_send(self):
        """机器码标签要标明无需发送，避免客户误以为必须发。"""
        from licensing import activation_ui
        src = inspect.getsource(activation_ui.show_activation_dialog)
        self.assertIn('无需发送', src, "机器码未标注「无需发送」")

    @unittest.skipUnless(os.environ.get("DISPLAY") or os.name == "nt",
                         "需要图形环境")
    def test_real_dialog_has_only_request_buttons(self):
        """真实对话框按钮里没有「复制机器码」，但有激活请求的复制/导出。"""
        import tkinter as tk
        from licensing import activation_ui

        root = tk.Tk()
        root.withdraw()
        captured = {}

        class _Stop(Exception):
            pass

        def break_wait(_self):
            root.update_idletasks()
            captured["buttons"] = _all_button_texts(root)
            raise _Stop()

        try:
            with mock.patch.object(root, "wait_window", break_wait), \
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
            buttons = captured.get("buttons", [])
            self.assertNotIn("复制机器码", buttons,
                             "对话框仍有多余的复制机器码按钮：%s" % buttons)
            self.assertTrue(any("激活请求" in b for b in buttons),
                            "缺少复制激活请求按钮：%s" % buttons)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
