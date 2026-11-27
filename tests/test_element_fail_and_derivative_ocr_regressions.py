"""元素失败返回 + 全模式衍生名 OCR 回归。

A. mode=5 挑战失败页无「下一关」会卡在 pending；须先认失败文案再点返回。
B. 3选1 / 4选2 最终决策须传 use_derivative_name=True（与 band_roi 解耦）。

本环境常无 pywin32，故用源码断言 + 抽方法绑定 stub，避免 import game_bot。
"""
from __future__ import annotations

import ast
import os
import textwrap
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME_BOT_PATH = os.path.join(ROOT, "game_bot.py")


def _game_bot_source():
    with open(GAME_BOT_PATH, encoding="utf-8") as fh:
        return fh.read()


def _method_source(class_name: str, method_name: str) -> str:
    """从 game_bot.py AST 抽出方法源码（含缩进）。"""
    tree = ast.parse(_game_bot_source())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return ast.get_source_segment(_game_bot_source(), item)
    raise AssertionError("找不到 %s.%s" % (class_name, method_name))


def _bind_method(method_name: str):
    """把 GameBot 上的方法绑定到轻量 stub（不触发 win32 导入）。"""
    src = _method_source("GameBot", method_name)
    wrapped = "class _Stub:\n" + textwrap.indent(src, "    ")
    ns = {"time": time}
    exec(compile(wrapped, GAME_BOT_PATH, "exec"), ns)
    return ns["_Stub"]()


class TestElementFailReturn(unittest.TestCase):
    def test_fail_templates_exist(self):
        for name in ("element-fail-text.png", "element-fail-reward.png"):
            path = os.path.join(ROOT, "templates", name)
            self.assertTrue(os.path.isfile(path), "缺少模板 %s" % path)

    def test_fail_return_requires_fail_text_before_click(self):
        """未命中挑战失败文案时不得点返回（成功结算也有返回）。"""
        bot = _bind_method("find_click_element_fail_return")
        bot.game_window = (100, 50, 544, 1011)
        with mock.patch.object(bot, "find_template", create=True, return_value=None) as ft, \
             mock.patch.object(bot, "click", create=True) as click:
            self.assertFalse(bot.find_click_element_fail_return())
            click.assert_not_called()
            ft.assert_called_with("element-fail-text.png", threshold=0.75)

    def test_fail_return_clicks_return_template_when_present(self):
        bot = _bind_method("find_click_element_fail_return")
        bot.game_window = (100, 50, 544, 1011)
        calls = []

        def fake_find(name, threshold=None):
            calls.append(name)
            if name == "element-fail-text.png":
                return (200, 180)
            if name == "return.png":
                return (372, 950)
            return None

        with mock.patch.object(bot, "find_template", create=True, side_effect=fake_find), \
             mock.patch.object(bot, "click", create=True) as click, \
             mock.patch.object(time, "sleep"):
            self.assertTrue(bot.find_click_element_fail_return())
            click.assert_called_once_with(372, 950)
        self.assertEqual(calls[0], "element-fail-text.png")
        self.assertIn("return.png", calls)

    def test_fail_return_falls_back_to_bottom_center(self):
        bot = _bind_method("find_click_element_fail_return")
        bot.game_window = (100, 50, 544, 1011)

        def fake_find(name, threshold=None):
            if name == "element-fail-text.png":
                return (200, 180)
            return None

        with mock.patch.object(bot, "find_template", create=True, side_effect=fake_find), \
             mock.patch.object(bot, "click", create=True) as click, \
             mock.patch.object(time, "sleep"):
            self.assertTrue(bot.find_click_element_fail_return())
            click.assert_called_once_with(100 + 272, 50 + 900)

    def test_mode5_loop_source_clears_pending_on_fail_return(self):
        """主循环在「下一关」之后插入失败返回，并清 pending / 累加 battle_count。"""
        src = _method_source("GameBot", "main_loop")
        self.assertIn("find_click_element_fail_return", src)
        idx_fail = src.index("find_click_element_fail_return")
        idx_next = src.index("find_click_element_next")
        self.assertLess(idx_next, idx_fail, "失败返回应在下一关之后")
        snippet = src[idx_fail:idx_fail + 500]
        self.assertIn("element_pending_battle = False", snippet)
        self.assertIn("element_after_next = False", snippet)
        self.assertIn("battle_count += 1", snippet)

    def test_mode5_reuses_receive_before_lobby_scroll(self):
        """元素 continue 跳过通用段，须在 mode=5 内复用领取/关闭，且仍躲开 find_click_return。"""
        src = _method_source("GameBot", "main_loop")
        # mode=5 块：从「元素试炼模式」到块外「点击领取」
        start = src.index("元素试炼模式")
        end = src.index("点击领取", start)
        block = src[start:end]
        self.assertIn("find_click_receive", block)
        self.assertIn("find_click_home_close", block)
        # 注释里会提到 find_click_return；断言只看真实调用
        self.assertNotIn("self.find_click_return(", block)
        self.assertLess(
            block.index("find_click_receive"),
            block.index("scroll_lobby_down"),
            "领取应在历练大厅下滑之前",
        )


class TestDerivativeNameOcrWiring(unittest.TestCase):
    def test_pick_one_always_passes_use_derivative_name(self):
        src = _method_source("GameBot", "find_click_skill_pick_one")
        self.assertIn("use_derivative_name=True", src)
        self.assertNotIn("use_derivative_name=band_roi", src)

    def test_abyss_passes_use_derivative_name_and_roi(self):
        src = _method_source("GameBot", "find_click_skill_abyss")
        self.assertIn("use_derivative_name=True", src)
        self.assertIn("derivative_roi=(50, -95, -70)", src)

    def test_preferred_slots_forwards_derivative_roi(self):
        """derivative_roi 须拆成 half_w/top/bottom 传给 _ocr_card_derivative。"""
        src = _method_source("GameBot", "_preferred_skill_slots")
        self.assertIn("derivative_roi", src)
        self.assertIn("half_w=d_hw", src)
        self.assertIn("top=d_top", src)
        self.assertIn("bottom=d_bot", src)


if __name__ == "__main__":
    unittest.main()
