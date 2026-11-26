"""中文安装路径回归测试（TDD 红 → 绿）。

现象：客户把程序装在中文目录（如 D:\\游戏\\向僵尸开炮助手）时，
game_bot.py 里 6 处 cv2.imread(中文路径) 在 Windows 上静默返回 None，
115 张模板全部加载失败，识别功能完全失效。

修复：在 launcher（发布入口）统一打补丁，让 cv2.imread 遇到非 ASCII
路径时改用 np.fromfile + cv2.imdecode，ASCII 路径保持原样，零侵入 game_bot.py。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestUnicodeImageRead(unittest.TestCase):
    def setUp(self):
        import numpy as np
        self.np = np
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 造一张真实 png
        import cv2
        img = self.np.zeros((8, 8, 3), dtype=self.np.uint8)
        img[:] = (10, 20, 30)
        self.png = os.path.join(self.tmp.name, "tpl.png")
        cv2.imwrite(self.png, img)

    def test_helper_reads_unicode_path(self):
        """补丁函数必须能读中文路径，且返回正确像素。"""
        import launcher
        self.assertTrue(hasattr(launcher, "imread_unicode"),
                        "launcher 缺少 imread_unicode 补丁函数")
        cn_dir = os.path.join(self.tmp.name, "中文 目录")
        os.makedirs(cn_dir)
        cn_png = os.path.join(cn_dir, "模板.png")
        shutil.copy(self.png, cn_png)
        img = launcher.imread_unicode(cn_png)
        self.assertIsNotNone(img, "中文路径读取失败")
        self.assertEqual(tuple(img[0, 0]), (10, 20, 30))

    def test_helper_reads_ascii_path(self):
        """ASCII 路径保持不变，能正常读取。"""
        import launcher
        img = launcher.imread_unicode(self.png)
        self.assertIsNotNone(img)
        self.assertEqual(tuple(img[0, 0]), (10, 20, 30))

    def test_helper_missing_file_returns_none(self):
        """文件不存在时返回 None，不抛异常。"""
        import launcher
        self.assertIsNone(launcher.imread_unicode(
            os.path.join(self.tmp.name, "不存在.png")))

    def test_patch_applied_to_cv2(self):
        """打补丁后 cv2.imread 本身能处理中文路径。"""
        import cv2
        import launcher
        cn_dir = os.path.join(self.tmp.name, "中文")
        os.makedirs(cn_dir)
        cn_png = os.path.join(cn_dir, "模板.png")
        shutil.copy(self.png, cn_png)
        launcher.patch_cv2_imread()
        try:
            img = cv2.imread(cn_png)
            self.assertIsNotNone(img, "cv2.imread 打补丁后仍读不了中文路径")
            self.assertEqual(tuple(img[0, 0]), (10, 20, 30))
        finally:
            launcher.restore_cv2_imread()

    def test_restore_restores_original(self):
        """恢复后 cv2.imread 回到原始实现（不影响其它测试）。"""
        import cv2
        import launcher
        before = cv2.imread
        launcher.patch_cv2_imread()
        launcher.restore_cv2_imread()
        self.assertIs(cv2.imread, before, "恢复后 cv2.imread 不是原函数")


if __name__ == "__main__":
    unittest.main()
