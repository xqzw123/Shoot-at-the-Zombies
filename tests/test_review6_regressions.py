"""第六轮 review 回归测试（TDD 红阶段）。

覆盖第五轮两路审查判定「暂不建议直接交付」的新必修项：

安全侧
* B-1 间歇使用下「阻断 NTP + 回拨本地时间」让 timed 到期复活
* H-1 锚/高水位自愈被反向利用：失败仍写盘、过去方向无交叉校验
* H-2 NTP「全部互相认同」实为 1 天极差，2/3 源投毒 <1 天即可放行
* M-1 frozen 产物漏打 state_secret 时缺少运行时自检

工程侧
* H1 作者端签发时间无 fail-closed，与客户端首次激活不对称
* H2 dist_problems 漏掉改名 / 非常规后缀的敏感文件
* M6 anchor_vcs_problems 把「仅暂存」误判为已纳入版本控制
* L1 心跳 start/stop 缺锁，可产生双线程双倍累计 used_seconds
"""
from __future__ import annotations

import importlib.util
import os
import threading
import time
import unittest
from unittest import mock

from licensing import clock, state_store, license_core, keygen
from tests.support import LicensingTestCase, BASE_TIME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_build_release():
    path = os.path.join(ROOT, "packaging", "build_release.py")
    spec = importlib.util.spec_from_file_location("sz_build_release_r6", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClockIsolatedMixin:
    def isolate_clock(self, ntp_enabled: bool = True):
        clock.set_test_clock(None)
        clock.set_ntp_enabled(ntp_enabled)
        clock.set_time_anchor(None)
        clock.reset_cache()

    def restore_clock(self):
        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()
        clock.stop_time_refresher()


class TestTimedExpiryCannotBeRevived(LicensingTestCase, ClockIsolatedMixin):
    """B-1：不删状态、不打补丁、不逆向，仅「断 NTP + 回拨时间」。"""

    def tearDown(self):
        self.restore_clock()
        super().tearDown()

    def _intermittent_state(self, days=30):
        """激活后只用了几小时就长期不开：last_seen / high_water 严重滞后。"""
        token = self.issue(typ="timed", days=days)
        self.assertTrue(self.activate(token)[0])
        state = state_store.load_state()
        state["last_seen"] = BASE_TIME + 2 * 86400
        state["used_seconds"] = 3600
        state_store.save_state(state)
        return token, state

    def test_blocking_ntp_and_rolling_back_time_cannot_start_expired_license(self):
        token, _ = self._intermittent_state(days=30)
        self.isolate_clock()
        # 真实时间已在 A+60 天；NTP 被防火墙全断，本机时间回拨到 A+29 天。
        # 注意 A+29 天比滞后的 last_seen（A+2 天）晚，旧回拨检测完全看不出来。
        with mock.patch("licensing.clock._ntp_seconds", return_value=None), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 29 * 86400):
            clock.refresh_now(force=True)
            ok, reason = license_core.check_license(strict_time=True)
        self.assertFalse(ok, "过期许可被「断网 + 回拨」复活了")
        self.assertIn("校时", reason)

    def test_high_water_is_a_floor_for_timed_expiry(self):
        """即便调用方绕过启动检查，历史最晚可信时间也不能被更小的 now 抹掉。"""
        token, state = self._intermittent_state(days=30)
        state["high_water"] = BASE_TIME + 60 * 86400
        state_store.save_state(state)
        ok, _, reason = license_core.validate(
            token, state=state_store.load_state(), now=BASE_TIME + 29 * 86400)
        self.assertFalse(ok)
        self.assertIn("到期", reason)

    def test_high_water_is_written_on_activation(self):
        token = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(token)[0])
        state = state_store.load_state()
        self.assertEqual(state.get("high_water"), BASE_TIME)

    def test_polluted_last_seen_heals_without_lowering_high_water(self):
        """诚实 NTP 修正被污染的 last_seen 时，单调高水位必须保持不减。"""
        token, state = self._intermittent_state(days=30)
        state["last_seen"] = BASE_TIME + 10 * 86400
        state["high_water"] = 0          # 模拟老版本状态：尚无单调高水位
        state_store.save_state(state)
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 3600), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 3600):
            clock.refresh_now(force=True)
            ok, reason = license_core.check_license()
            self.assertTrue(ok, reason)
        loaded = state_store.load_state()
        self.assertLess(loaded["last_seen"], BASE_TIME + 10 * 86400)
        self.assertEqual(loaded["high_water"], BASE_TIME + 3600)

    def test_high_water_is_never_lowered_when_present(self):
        """已有高水位的状态，任何自愈都不得把它拉低。"""
        token, state = self._intermittent_state(days=30)
        state["last_seen"] = BASE_TIME + 10 * 86400
        state["high_water"] = BASE_TIME + 10 * 86400
        state_store.save_state(state)
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 3600), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 3600):
            clock.refresh_now(force=True)
            license_core.check_license()
        loaded = state_store.load_state()
        self.assertEqual(loaded["high_water"], BASE_TIME + 10 * 86400)
        self.assertGreaterEqual(loaded["last_seen"], BASE_TIME + 10 * 86400)

    def test_failed_revalidation_does_not_persist_lowered_last_seen(self):
        """自愈重验失败时不得把下调过的 last_seen 写盘（H-1）。"""
        token, state = self._intermittent_state(days=30)
        state["last_seen"] = BASE_TIME + 10 * 86400
        state["high_water"] = BASE_TIME + 10 * 86400
        state["used_seconds"] = 31 * 86400   # 重验必定失败
        state_store.save_state(state)
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 3600), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 3600):
            clock.refresh_now(force=True)
            license_core.check_license()
        loaded = state_store.load_state()
        self.assertEqual(loaded["last_seen"], BASE_TIME + 10 * 86400,
                         "重验失败却把被下调的高水位写盘了")

    def test_perpetual_is_not_blocked_by_strict_time_check(self):
        """买断版没有时间权益，不应因 NTP 不可达被误伤。"""
        token = self.issue(typ="perpetual")
        self.assertTrue(self.activate(token)[0])
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds", return_value=None), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 29 * 86400):
            clock.refresh_now(force=True)
            ok, reason = license_core.check_license(strict_time=True)
        self.assertTrue(ok, reason)

class TestNtpConsensusHardening(LicensingTestCase, ClockIsolatedMixin):
    def tearDown(self):
        self.restore_clock()
        super().tearDown()

    def test_two_of_three_poisoned_within_one_day_rejected(self):
        """H-2：极差 ≤1 天的旧判据会让 20 小时偏移的 2/3 源投毒成为共识。"""
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds",
                        side_effect=[BASE_TIME, BASE_TIME + 20 * 3600,
                                     BASE_TIME + 20 * 3600]):
            self.assertIsNone(clock.ntp_consensus())

    def test_two_reachable_samples_with_one_poison_rejected(self):
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds",
                        side_effect=[BASE_TIME - 20 * 3600, BASE_TIME, None]):
            self.assertIsNone(clock.ntp_consensus())

    def test_poisoned_past_ntp_with_honest_local_clock_is_not_cross_checked(self):
        """H-1：多源一致地落在过去，但本机时钟诚实 → 不得当成可信时间。"""
        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME)
        with mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME - 30 * 86400), \
             mock.patch("licensing.clock.time.time", return_value=BASE_TIME):
            clock.refresh_now(force=True)
            self.assertFalse(clock.time_is_cross_checked())

    def test_honest_consensus_with_honest_local_clock_is_cross_checked(self):
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 60):
            clock.refresh_now(force=True)
            self.assertTrue(clock.time_is_cross_checked())

    def test_ntp_unavailable_is_not_cross_checked(self):
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds", return_value=None), \
             mock.patch("licensing.clock.time.time", return_value=BASE_TIME):
            clock.refresh_now(force=True)
            self.assertFalse(clock.time_is_cross_checked())


class TestSigningTimeFailClosed(LicensingTestCase, ClockIsolatedMixin):
    def tearDown(self):
        self.restore_clock()
        super().tearDown()

    def test_issue_refuses_when_ntp_unavailable(self):
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds", return_value=None), \
             mock.patch("licensing.clock.time.time", return_value=BASE_TIME):
            with self.assertRaises(RuntimeError) as ctx:
                keygen.issue("A002", self.request(), "timed", 7 * 86400)
        self.assertIn("校时", str(ctx.exception))

    def test_issue_refuses_when_local_clock_disagrees_with_ntp(self):
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 200 * 86400):
            with self.assertRaises(RuntimeError):
                keygen.issue("A002", self.request(), "timed", 7 * 86400)

    def test_issue_uses_cross_checked_ntp_time(self):
        from licensing import rsa_sign
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 60):
            token = keygen.issue("A002", self.request(), "timed", 7 * 86400)
        ok, payload, _ = rsa_sign.verify_token(token, self.pub)
        self.assertTrue(ok)
        self.assertEqual(payload["iat"], BASE_TIME)

    def test_revocation_refuses_when_ntp_unavailable(self):
        self.issue(typ="timed", days=7)
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds", return_value=None), \
             mock.patch("licensing.clock.time.time", return_value=BASE_TIME):
            with self.assertRaises(RuntimeError):
                keygen.make_revocation("A001", "用户退款")


class TestReleaseGuardsRound6(LicensingTestCase):
    def test_dist_problems_flags_renamed_sensitive_files(self):
        br = load_build_release()
        with mock.patch.object(br, "OUT_DIR", self.tmp.name):
            for name in ("private2.json", "orders_2026.jsonl",
                         "state_secret_backup.txt", "bot.log", "debug.enc",
                         "revocation.revoke.bak", "customers.csv",
                         "keygen_notes.md"):
                with open(os.path.join(self.tmp.name, name), "w",
                          encoding="utf-8") as fh:
                    fh.write("x")
            problems = br.dist_problems()
        self.assertTrue(problems, "改名/非常规后缀的敏感文件漏检")

    def test_dist_problems_allows_real_third_party_layout(self):
        br = load_build_release()
        root = self.tmp.name
        rels = (
            "jiangshikaipao.exe",
            os.path.join("_internal", "VCRUNTIME140.dll"),
            os.path.join("_internal", "_socket.pyd"),
            os.path.join("_internal", "base_library.zip"),
            os.path.join("_internal", "cv2", "config.py"),
            os.path.join("_internal", "_tcl_data", "encoding", "ascii.enc"),
            os.path.join("_internal", "_tcl_data", "tzdata", "UTC"),
            os.path.join("_internal", "numpy-2.5.5.dist-info", "METADATA"),
            os.path.join("_internal", "numpy-2.5.5.dist-info", "licenses", "LICENSE.md"),
            os.path.join("_internal", "templates", "shot.png"),
        )
        with mock.patch.object(br, "OUT_DIR", root):
            for rel in rels:
                path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as fh:
                    fh.write(b"x")
            self.assertEqual(br.dist_problems(), [])

    def test_anchor_vcs_check_uses_head_not_index(self):
        """仅 git add 不算纳入版本控制，干净 clone 里仍拿不到锚。"""
        br = load_build_release()
        with mock.patch.object(br.subprocess, "run",
                               return_value=mock.Mock(returncode=1)) as run:
            problems = br.anchor_vcs_problems()
        self.assertTrue(problems)
        command = run.call_args[0][0]
        self.assertIn("HEAD", " ".join(str(x) for x in command),
                      "应查 HEAD（cat-file/ls-tree），不能只查 index")

    def test_anchor_vcs_check_passes_when_in_head(self):
        br = load_build_release()
        with mock.patch.object(br.subprocess, "run",
                               return_value=mock.Mock(returncode=0)):
            self.assertEqual(br.anchor_vcs_problems(), [])

    def test_launcher_verifies_release_state_secret_at_runtime(self):
        import launcher
        with mock.patch.object(launcher, "sys") as fake_sys, \
             mock.patch.object(launcher.state_store,
                               "state_secret_is_release", return_value=False), \
             mock.patch.object(launcher, "_report_startup_error") as report:
            fake_sys.frozen = True
            fake_sys.__compiled__ = None
            self.assertFalse(launcher._verify_release_secrets())
            self.assertTrue(report.called)

    def test_legacy_build_bat_stops_before_packaging(self):
        with open(os.path.join(ROOT, "build_exe.bat"), "r",
                  encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("exit /b 1", content.lower(),
                      "DEPRECATED 脚本仍能产出无授权 exe")


class TestRound6FollowUps(LicensingTestCase):
    """第六轮工程复审列出的「修复未到位」项，按 TDD 补行为断言。"""

    def test_dist_problems_flags_project_py_in_generic_dirs(self):
        """通用目录名（share/licenses）不能当成第三方白名单放行改名源码。"""
        br = load_build_release()
        root = self.tmp.name
        rels = (
            os.path.join("share", "secret.py"),
            os.path.join("licenses", "bot_core.py"),
            os.path.join("_internal", "share", "secret.py"),
        )
        with mock.patch.object(br, "OUT_DIR", root):
            for rel in rels:
                path = os.path.join(root, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("x")
            problems = br.dist_problems()
        self.assertTrue(problems, "改名后的项目源码被通用目录白名单放行")

    def test_anchor_vcs_skips_anchor_outside_repo(self):
        """锚在仓库外（跨盘符临时目录）时不能把 relpath 错误谎报成未提交。"""
        br = load_build_release()
        outside = "Z:\\definitely-outside\\state_secret.fingerprint"
        with mock.patch.object(br, "STATE_SECRET_FINGERPRINT", outside), \
             mock.patch.object(br.subprocess, "run") as run:
            problems = br.anchor_vcs_problems()
        self.assertEqual(problems, [])
        self.assertFalse(run.called)

    def test_keygen_gui_revoke_reports_untrusted_time(self):
        """GUI 生成撤销令时 NTP 不可信不能让异常从 Tk 回调逸出。"""
        from licensing import keygen_ui
        win = keygen_ui.KeygenWindow.__new__(keygen_ui.KeygenWindow)
        win.machine_var = mock.Mock()
        win.machine_var.get.return_value = "A001"
        win.status = mock.Mock()
        win._set_output = mock.Mock()
        win.show_ledger = mock.Mock()
        with mock.patch.object(keygen_ui.keygen, "make_revocation",
                               side_effect=RuntimeError("签发时间不可信")), \
             mock.patch.object(keygen_ui.messagebox, "showerror") as show:
            win.do_revoke()
        self.assertTrue(show.called, "GUI 未把 fail-closed 错误显示给作者")
        self.assertFalse(win._set_output.called)

    def test_ensure_license_passes_strict_time_flag(self):
        """行为断言：启动入口必须调用严格校时，而不是只查源码字符串。"""
        with mock.patch.object(license_core, "check_license",
                               return_value=(True, "OK")) as check:
            self.assertTrue(license_core.ensure_license())
        check.assert_called_once_with(strict_time=True)


class TestActivationUiRevocationRobustness(LicensingTestCase):
    """客户端导入撤销令时，异常必须从 Tk 回调内部收敛，不能逸出。"""

    def test_revocation_io_error_shows_messagebox(self):
        from licensing import activation_ui
        status_set = mock.Mock()
        show_receipt = mock.Mock()
        with mock.patch.object(activation_ui.license_core, "apply_revocation",
                               side_effect=OSError("磁盘写失败")), \
             mock.patch.object(activation_ui.messagebox, "showerror") as show:
            activation_ui._import_revocation("SZL1.bad", status_set, show_receipt)
        self.assertTrue(show.called, "异常未提示给客户")
        self.assertFalse(show_receipt.called)

    def test_revocation_success_still_shows_receipt(self):
        from licensing import activation_ui
        status_set = mock.Mock()
        show_receipt = mock.Mock()
        receipt = {"order": "A001"}
        with mock.patch.object(activation_ui.license_core, "apply_revocation",
                               return_value=(True, receipt, "OK")):
            activation_ui._import_revocation("SZL1.ok", status_set, show_receipt)
        show_receipt.assert_called_once_with(receipt)
        self.assertIn("停用", status_set.call_args[0][0])

    def test_revocation_rejection_updates_status(self):
        from licensing import activation_ui
        status_set = mock.Mock()
        show_receipt = mock.Mock()
        with mock.patch.object(activation_ui.license_core, "apply_revocation",
                               return_value=(False, None, "签名无效")):
            activation_ui._import_revocation("SZL1.bad", status_set, show_receipt)
        self.assertIn("签名无效", status_set.call_args[0][0])
        self.assertFalse(show_receipt.called)


class TestHeartbeatStartLock(LicensingTestCase):
    def test_concurrent_start_creates_only_one_heartbeat(self):
        created = []
        real_thread = threading.Thread

        class CountingThread(real_thread):
            def __init__(self, *args, **kwargs):
                created.append(kwargs.get("name"))
                super().__init__(*args, **kwargs)

            def start(self):
                time.sleep(0.03)   # 放大「检查 is_alive → 赋值」窗口
                super().start()

        with mock.patch.object(license_core.threading, "Thread", CountingThread):
            # 用未打补丁的 Thread 构造并发调用者，否则它们也会被计入 created。
            starters = [real_thread(target=license_core.start_heartbeat)
                        for _ in range(2)]
            for t in starters:
                t.start()
            for t in starters:
                t.join(timeout=2)
        self.assertEqual(len(created), 1,
                         "并发 start_heartbeat 起了 %d 个线程" % len(created))


class TestOcrRuntimePackaging(LicensingTestCase):
    """新版 game_bot.py 用 rapidocr-onnxruntime 选技能，模型必须随包发布。"""

    def test_build_command_includes_rapidocr_resources(self):
        """构建命令必须把 rapidocr 包目录作为数据文件打进去。"""
        br = load_build_release()
        cmd = br.pyinstaller_cmd()
        joined = " ".join(str(x) for x in cmd)
        self.assertIn("rapidocr", joined.lower(),
                      "构建命令未包含 rapidocr 数据文件，客户机 OCR 会失败")

    def test_preflight_checks_ocr_resources(self):
        """发布前检查要能发现缺失的 OCR 模型。"""
        br = load_build_release()
        self.assertTrue(hasattr(br, "ocr_resource_problems"))
        problems = br.ocr_resource_problems()
        self.assertEqual(problems, [], "当前打包环境的 OCR 资源应完整：%s" % problems)

    def test_dist_allows_ocr_models_but_still_rejects_py(self):
        """rapidocr 目录只放行 .onnx/.yaml 数据文件，塞 .py 仍须被拦。"""
        import tempfile
        br = load_build_release()
        with tempfile.TemporaryDirectory() as tmp:
            ocr_dir = os.path.join(tmp, "_internal", "rapidocr_onnxruntime", "models")
            os.makedirs(ocr_dir)
            with open(os.path.join(ocr_dir, "det.onnx"), "wb") as fh:
                fh.write(b"x")
            with mock.patch.object(br, "OUT_DIR", tmp):
                self.assertEqual(br.dist_problems(), [])
                with open(os.path.join(ocr_dir, "evil.py"), "wb") as fh:
                    fh.write(b"x")
                problems = br.dist_problems()
            self.assertTrue(any("evil.py" in p for p in problems),
                            "rapidocr 目录放了 .py 未被拦：%s" % problems)

    def test_built_dist_contains_onnx_models(self):
        """已构建产物里必须有 .onnx 模型，否则选技能功能失效。"""
        br = load_build_release()
        if not os.path.isdir(br.OUT_DIR):
            self.skipTest("尚未构建")
        models = []
        for base, _dirs, files in os.walk(br.OUT_DIR):
            for name in files:
                if name.lower().endswith(".onnx"):
                    models.append(os.path.relpath(os.path.join(base, name), br.OUT_DIR))
        self.assertTrue(models, "发布包缺少 OCR 模型文件，客户机选技能会失效")


if __name__ == "__main__":
    unittest.main()
