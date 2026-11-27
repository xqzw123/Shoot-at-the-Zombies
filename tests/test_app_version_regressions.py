"""VERSION 单一源：读取、bump、iss 同步、窗口标题。"""
from __future__ import annotations

import os
import tempfile
import unittest

import app_version


class TestAppVersion(unittest.TestCase):
    def test_repo_version_file_readable(self):
        ver = app_version.read_version()
        self.assertRegex(ver, r"^\d+\.\d+\.\d+$")

    def test_window_title_includes_version(self):
        title = app_version.window_title("1.2.3")
        self.assertEqual(title, "向僵尸开炮助手 v1.2.3")

    def test_bump_patch(self):
        self.assertEqual(app_version.bump_patch("1.0.0"), "1.0.1")
        self.assertEqual(app_version.bump_patch("1.2.9"), "1.2.10")

    def test_bump_patch_rejects_garbage(self):
        with self.assertRaises(ValueError):
            app_version.bump_patch("1.0")

    def test_sync_installer_iss(self):
        with tempfile.TemporaryDirectory() as tmp:
            iss = os.path.join(tmp, "installer.iss")
            with open(iss, "w", encoding="utf-8") as fh:
                fh.write('#define MyAppName "x"\n#define MyAppVersion "1.0.0"\n')
            app_version.sync_installer_iss("1.0.5", iss_path=iss)
            text = open(iss, encoding="utf-8").read()
            self.assertIn('#define MyAppVersion "1.0.5"', text)
            self.assertNotIn('"1.0.0"', text)

    def test_write_and_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "VERSION")
            app_version.write_version("9.8.7", path=path)
            self.assertEqual(open(path, encoding="utf-8").read(), "9.8.7\n")

    def test_pyinstaller_packs_version_file(self):
        import importlib.util
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "packaging", "build_release.py",
        )
        spec = importlib.util.spec_from_file_location("sz_br_ver", path)
        br = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(br)
        joined = " ".join(str(x) for x in br.pyinstaller_cmd())
        self.assertIn("VERSION", joined)
        self.assertIn("version", br._ALLOWED_PROJECT_FILES)


if __name__ == "__main__":
    unittest.main()
