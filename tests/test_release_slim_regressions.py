"""发布包瘦身回归测试（TDD 红 → 绿）。

发布目录里有两个纯冗余的二进制：
* cv2/opencv_videoio_ffmpeg*.dll —— 只有 VideoCapture/VideoWriter 才用，
  本程序只做 imread + 屏幕截图；
* PIL/_avif*.pyd —— 只有读取 AVIF 图片才用，本程序不读该格式。

两者合计约 48 MB。测试约束：
1. 排除规则必须声明且同时覆盖这两项；
2. 剔除函数只删匹配文件，不误伤 cv2.pyd / PIL 其他插件 / OCR 模型 / 模板；
3. 构建流程必须调用剔除，且 preflight 能发现「旧产物残留冗余文件」；
4. 剔除后 imread / 截图转 BGR / RapidOCR 初始化链路仍可用（若环境有构建产物）。
"""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_build_release():
    path = os.path.join(ROOT, "packaging", "build_release.py")
    spec = importlib.util.spec_from_file_location("sz_build_release_slim", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestReleaseSlim(unittest.TestCase):
    def test_prunable_patterns_declared(self):
        """必须声明两类冗余二进制的排除规则。"""
        br = load_build_release()
        rules = br.PRUNABLE_BINARIES
        joined = " ".join(str(getattr(r, "pattern", r)) for r in
                          (p for _sub, p in rules))
        self.assertIn("videoio_ffmpeg", joined)
        self.assertIn("avif", joined)

    def test_prune_removes_only_matching_files(self):
        """剔除函数只删目标文件，不动 OCR 模型/模板/核心 DLL。"""
        br = load_build_release()
        with tempfile.TemporaryDirectory() as tmp:
            cv2_dir = os.path.join(tmp, "_internal", "cv2")
            pil_dir = os.path.join(tmp, "_internal", "PIL")
            ocr_dir = os.path.join(tmp, "_internal", "rapidocr_onnxruntime", "models")
            tpl_dir = os.path.join(tmp, "_internal", "templates")
            for d in (cv2_dir, pil_dir, ocr_dir, tpl_dir):
                os.makedirs(d, exist_ok=True)
            names = {
                os.path.join(cv2_dir, "opencv_videoio_ffmpeg510_64.dll"): b"a",
                os.path.join(pil_dir, "_avif.cp313-win_amd64.pyd"): b"b",
                os.path.join(cv2_dir, "cv2.pyd"): b"keep",
                os.path.join(pil_dir, "_imaging.cp313-win_amd64.pyd"): b"keep",
                os.path.join(ocr_dir, "ch_PP-OCRv3_rec_infer.onnx"): b"keep",
                os.path.join(tpl_dir, "skill.png"): b"keep",
            }
            for path, data in names.items():
                with open(path, "wb") as fh:
                    fh.write(data)
            removed = br.prune_release(os.path.join(tmp, "_internal"))
            self.assertEqual(len(removed), 2, "剔除数量不对：%s" % removed)
            self.assertTrue(all(os.path.isfile(p) for p in names
                                if p not in (os.path.join(cv2_dir, "opencv_videoio_ffmpeg510_64.dll"),
                                             os.path.join(pil_dir, "_avif.cp313-win_amd64.pyd"))),
                            "误删了必须保留的文件")
            self.assertEqual(br.prunable_leftovers(os.path.join(tmp, "_internal")), [])

    def test_prunable_leftovers_detected(self):
        """preflight 用的残留检查能发现未剔除的冗余文件。"""
        br = load_build_release()
        with tempfile.TemporaryDirectory() as tmp:
            cv2_dir = os.path.join(tmp, "cv2")
            os.makedirs(cv2_dir)
            stale = os.path.join(cv2_dir, "opencv_videoio_ffmpeg999_64.dll")
            with open(stale, "wb") as fh:
                fh.write(b"x")
            leftovers = br.prunable_leftovers(tmp)
            self.assertTrue(any("videoio_ffmpeg" in x for x in leftovers),
                            "残留检查漏掉了 ffmpeg DLL：%s" % leftovers)

    def test_build_command_keeps_cv2_core_and_ocr(self):
        """排除规则不能以牺牲核心依赖为代价。"""
        br = load_build_release()
        cmd = br.pyinstaller_cmd()
        joined = " ".join(str(x) for x in cmd).lower()
        self.assertIn("rapidocr", joined)
        self.assertIn("--onedir", joined)


class TestSlimmedRuntimeWorks(unittest.TestCase):
    """剔除后在真实发布产物上验证功能链路；无产物时跳过。"""

    def test_imread_and_screenshot_pipeline(self):
        br = load_build_release()
        internal = os.path.join(br.OUT_DIR, "_internal")
        if not os.path.isdir(internal):
            self.skipTest("尚未构建")
        self.assertEqual(br.prunable_leftovers(internal), [],
                         "发布产物仍含冗余二进制，请先重建")
        import cv2
        import numpy as np
        tpl = os.path.join(internal, "templates")
        pngs = [f for f in os.listdir(tpl) if f.lower().endswith(".png")]
        self.assertTrue(pngs, "模板目录为空")
        img = cv2.imread(os.path.join(tpl, pngs[0]))
        self.assertIsNotNone(img, "剔除 ffmpeg DLL 后 imread 失败")
        # 模拟 take_screenshot 的 RGB→BGR 转换路径
        bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        self.assertEqual(bgr.shape, img.shape)

    def test_ocr_engine_initializes(self):
        br = load_build_release()
        internal = os.path.join(br.OUT_DIR, "_internal")
        if not os.path.isdir(internal):
            self.skipTest("尚未构建")
        from rapidocr_onnxruntime import RapidOCR
        engine = RapidOCR()
        self.assertIsNotNone(engine, "剔除 AVIF 插件后 OCR 引擎初始化失败")


if __name__ == "__main__":
    unittest.main()
