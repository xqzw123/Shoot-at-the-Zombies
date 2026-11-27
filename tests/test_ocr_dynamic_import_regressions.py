"""OCR 动态导入模块打包回归测试（TDD 红阶段）。

线上现象：客户激活后运行，日志报
    [OCR] 初始化失败，衍生名识别不可用: module 'ch_ppocr_v3_det' has no attribute 'TextDetector'

根因：rapidocr_onnxruntime/rapid_ocr_api.py 通过 config.yaml 里的 module_name
字符串动态 import 三个子包（ch_ppocr_v3_det / ch_ppocr_v3_rec / ch_ppocr_v2_cls），
PyInstaller 静态分析看不到；--collect-data 只收了 config.yaml，.py 没进 PYZ，
运行时 import 到的是只有 yaml 的 namespace package，故无 TextDetector。

修复：--paths 指向 rapidocr 包目录 + 三个子包显式 --hidden-import。
本测试锁定该配置，防止后续改打包脚本时再次回归。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from tests.support import LicensingTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OCR_MODULES = ("ch_ppocr_v3_det", "ch_ppocr_v3_rec", "ch_ppocr_v2_cls")


def load_build_release():
    import importlib.util
    path = os.path.join(ROOT, "packaging", "build_release.py")
    spec = importlib.util.spec_from_file_location("sz_build_release_ocrdyn", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOcrDynamicImportPackaging(LicensingTestCase):
    def test_build_command_hidden_imports_all_dynamic_ocr_modules(self):
        """三个动态导入的 OCR 子包必须显式 hidden-import，否则产物里无 TextDetector。"""
        br = load_build_release()
        cmd = [str(x) for x in br.pyinstaller_cmd()]
        for mod in OCR_MODULES:
            self.assertIn(mod, cmd,
                          "构建命令缺少 --hidden-import %s，客户机 OCR 会初始化失败" % mod)

    def test_build_command_adds_rapidocr_package_to_search_path(self):
        """PyInstaller 分析阶段需能定位顶层 ch_ppocr_* 包，须把 rapidocr 目录加入 --paths。"""
        br = load_build_release()
        cmd = [str(x) for x in br.pyinstaller_cmd()]
        self.assertIn("--paths", cmd, "构建命令缺少 --paths，hidden-import 找不到顶层 OCR 子包")
        idx = cmd.index("--paths")
        self.assertLess(idx + 1, len(cmd), "--paths 后缺少路径值")
        self.assertIn("rapidocr_onnxruntime", cmd[idx + 1],
                      "--paths 未指向 rapidocr 包目录：%s" % cmd[idx + 1])

    def test_preflight_exposes_dynamic_import_check(self):
        """发布前检查必须暴露动态导入模块的校验入口。"""
        br = load_build_release()
        self.assertTrue(hasattr(br, "ocr_dynamic_import_problems"),
                        "build_release.py 缺少 ocr_dynamic_import_problems 检查")
        self.assertEqual(br.ocr_dynamic_import_problems(), [],
                         "当前构建配置下 OCR 动态导入检查未通过：%s"
                         % br.ocr_dynamic_import_problems())

    def test_built_exe_pyz_contains_dynamic_ocr_modules(self):
        """产物级验证：exe 内 PYZ 必须真含三个顶层子包及其实现模块。

        只看构建命令会假绿（原 OCR 测试就是只查了模型文件），这里直接读
        PyInstaller CArchive 里的 PYZ 目录，确认 frozen 运行时 import 得到
        TextDetector / TextRecognizer / TextClassifier。
        """
        br = load_build_release()
        exe = os.path.join(br.OUT_DIR, br.APP_NAME + ".exe")
        if not os.path.isfile(exe):
            self.skipTest("尚未构建")

        import tempfile
        from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

        archive = CArchiveReader(exe)
        pyz_entries = [k for k in archive.toc if "PYZ" in str(k)]
        self.assertTrue(pyz_entries, "产物 exe 内找不到 PYZ")
        pyz_bytes = archive.extract(pyz_entries[0])

        with tempfile.NamedTemporaryFile(suffix=".pyz", delete=False) as fh:
            fh.write(pyz_bytes)
            pyz_path = fh.name
        try:
            toc = ZlibArchiveReader(pyz_path).toc
            for mod in OCR_MODULES:
                self.assertIn(mod, toc,
                              "产物 PYZ 缺顶层包 %s，运行时 TextDetector 不可用" % mod)
                self.assertTrue(any(k.startswith(mod + ".") for k in toc),
                                "产物 PYZ 缺 %s 的实现子模块" % mod)
        finally:
            os.unlink(pyz_path)

    def test_dynamic_import_check_catches_missing_hidden_import(self):
        """护栏自身要有效：模拟漏掉一个 hidden-import 必须报错。"""
        br = load_build_release()
        broken = [str(x) for x in br.pyinstaller_cmd()]
        broken.remove("ch_ppocr_v3_det")
        with mock.patch.object(br, "pyinstaller_cmd", return_value=broken):
            problems = br.ocr_dynamic_import_problems()
        self.assertTrue(any("ch_ppocr_v3_det" in p for p in problems),
                        "漏掉 det 子包未被护栏发现：%s" % problems)


if __name__ == "__main__":
    unittest.main()
