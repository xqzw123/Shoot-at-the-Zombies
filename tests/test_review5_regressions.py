"""第五轮 review 回归测试（TDD 红阶段）。

覆盖第四轮两路审查判定为「暂不建议直接交付」的 BLOCKER 与主要 HIGH：

* 心跳无条件写 ``last_seen`` + 锚在 1~30 天倒退时拒绝共识 → 付费客户被锁死
* 伪 NTP 共识（hosts 劫持 / 2 源投毒 + 1 源不可达）绕过首次激活
* 指纹锚未纳入版本控制，`ensure_state_secret` 在锚缺失时静默生成新密钥
* 看门狗异常静默死亡、回拨类失败被无限豁免
* 产物扫描只按文件名、`--check` 不校验 PYZ 内的状态密钥
"""
from __future__ import annotations

import importlib.util
import os
import unittest
from unittest import mock

from licensing import clock, state_store, license_core, keygen
from tests.support import LicensingTestCase, BASE_TIME

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_build_release():
    path = os.path.join(ROOT, "packaging", "build_release.py")
    spec = importlib.util.spec_from_file_location("sz_build_release_r5", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestHeartbeatAndAnchorHealing(LicensingTestCase):
    def tearDown(self):
        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()
        super().tearDown()

    def test_heartbeat_tick_accumulates_used_seconds(self):
        """行为断言：一次心跳必须真实累计 monotonic 运行时长。"""
        token = self.issue(typ="timed", days=30)
        self.assertTrue(self.activate(token)[0])
        license_core._heartbeat_tick(60)
        self.assertEqual(state_store.load_state()["used_seconds"], 60)

    def test_heartbeat_does_not_raise_last_seen_when_time_untrusted(self):
        token = self.issue(typ="timed", days=30)
        self.assertTrue(self.activate(token)[0])
        state = state_store.load_state()
        state["last_seen"] = BASE_TIME
        state_store.save_state(state)

        clock.set_test_clock(None)
        clock.set_ntp_enabled(False)
        clock.reset_cache()
        with mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 10 * 86400):
            license_core._heartbeat_tick(60)
        loaded = state_store.load_state()
        self.assertEqual(loaded["last_seen"], BASE_TIME)
        self.assertEqual(loaded["used_seconds"], 60)

    def test_heartbeat_updates_last_seen_when_time_trusted(self):
        token = self.issue(typ="timed", days=30)
        self.assertTrue(self.activate(token)[0])
        clock.set_test_clock(lambda: BASE_TIME + 3600)
        license_core._heartbeat_tick(60)
        self.assertEqual(state_store.load_state()["last_seen"], BASE_TIME + 3600)

    def test_polluted_anchor_heals_for_ten_day_skew(self):
        """旧实现只在落后 >30 天时自愈，10 天污染会让诚实 NTP 永远进不来。"""
        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(BASE_TIME + 10 * 86400)
        clock.reset_cache()
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME):
            self.assertEqual(clock.ntp_consensus(), BASE_TIME)

    def test_check_license_self_heals_polluted_last_seen(self):
        token = self.issue(typ="timed", days=30)
        self.assertTrue(self.activate(token)[0])
        state = state_store.load_state()
        state["last_seen"] = BASE_TIME + 10 * 86400
        state_store.save_state(state)

        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()
        with mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 3600), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 3600):
            clock.refresh_now(force=True)  # 模拟启动预热，先建立可信缓存
            ok, reason = license_core.check_license()
        self.assertTrue(ok, reason)
        self.assertLess(state_store.load_state()["last_seen"],
                        BASE_TIME + 10 * 86400)


class TestActivationClockCrossCheck(LicensingTestCase):
    def tearDown(self):
        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()
        super().tearDown()

    def test_activation_rejects_large_local_clock_skew(self):
        """只靠 hosts 投毒 NTP、不改本机系统时间时，大偏差应被交叉校验挡住。"""
        token = self.issue(typ="timed", days=7)
        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 200 * 86400):
            ok, _, reason = license_core.activate(token)
        self.assertFalse(ok)
        self.assertIn("校准", reason)
        self.assertIsNone(state_store.load_state())

    def test_activation_allows_small_local_clock_skew(self):
        token = self.issue(typ="timed", days=7)
        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 3600):
            ok, _, reason = license_core.activate(token)
        self.assertTrue(ok, reason)


class TestStateSecretAnchorGate(LicensingTestCase):
    def test_ensure_refuses_generation_when_anchor_missing_by_default(self):
        keys = os.path.join(self.tmp.name, "keys")
        module = os.path.join(self.tmp.name, "state_secret.py")
        anchor = os.path.join(self.tmp.name, "missing.fingerprint")
        with self.assertRaises(RuntimeError):
            keygen.ensure_state_secret(keys, module, fingerprint_path=anchor)

    def test_ensure_generates_only_when_explicitly_allowed(self):
        keys = os.path.join(self.tmp.name, "keys")
        module = os.path.join(self.tmp.name, "state_secret.py")
        anchor = os.path.join(self.tmp.name, "fingerprint.txt")
        secret = keygen.ensure_state_secret(keys, module,
                                            fingerprint_path=anchor,
                                            allow_generate=True)
        self.assertEqual(len(secret), 64)

    def test_check_warns_when_anchor_not_tracked_by_git(self):
        """行为断言：查不到 HEAD 里的锚时必须给出警告，不能静默返回 []。"""
        br = load_build_release()
        with mock.patch.object(br.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            problems = br.anchor_vcs_problems()
        self.assertTrue(problems)
        self.assertTrue(any("HEAD" in p for p in problems), problems)


class TestWatchdogRobustness(LicensingTestCase):
    def test_watchdog_survives_check_license_exception(self):
        import launcher
        root = mock.Mock()
        scheduled = []
        root.after.side_effect = lambda delay, fn: scheduled.append((delay, fn))
        with mock.patch.object(launcher.license_core, "check_license",
                               side_effect=RuntimeError("boom")):
            watch = launcher._make_watchdog(root, mock.Mock())
            watch()
        self.assertEqual(len(scheduled), 1, "看门狗一次异常后静默死亡")

    def test_rollback_exemption_is_bounded(self):
        import launcher
        root = mock.Mock()
        root.after.side_effect = lambda delay, fn: None
        gui = mock.Mock()
        with mock.patch.object(launcher.license_core, "check_license",
                               return_value=(False, "系统时间异常回拨，请校准时间后重试")):
            watch = launcher._make_watchdog(root, gui)
            for _ in range(20):
                watch()
        gui.quit_app.assert_called_once_with()


class TestReleaseGuardDepth(LicensingTestCase):
    def test_dist_problems_detects_renamed_or_plaintext_sources(self):
        br = load_build_release()
        with mock.patch.object(br, "OUT_DIR", self.tmp.name):
            for name in ("evil.py", "state_secret.py", "private_key.pem"):
                with open(os.path.join(self.tmp.name, name), "w",
                          encoding="utf-8") as fh:
                    fh.write("x")
            problems = br.dist_problems()
        self.assertTrue(problems, "按文件名精确匹配漏掉了明文源码/改名私钥")

    def test_pyz_state_secret_check_rejects_corrupt_exe(self):
        """行为断言：损坏/伪造的 exe 不能被 PYZ 校验静默放行。"""
        br = load_build_release()
        with mock.patch.object(br, "OUT_DIR", self.tmp.name):
            with open(os.path.join(self.tmp.name, br.APP_NAME + ".exe"),
                      "wb") as fh:
                fh.write(b"not a PyInstaller archive")
            problems = br.pyz_state_secret_problems()
        self.assertTrue(problems, "损坏的 exe 静默通过了 PYZ 状态密钥校验")

    def test_ntp_consensus_ignores_local_clock_baseline(self):
        """行为断言：本地时钟被回拨 200 天也不能改变 NTP 共识结果。"""
        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME - 200 * 86400):
            self.assertEqual(clock.ntp_consensus(), BASE_TIME)

    def test_installer_deletes_stale_internal_but_not_state(self):
        with open(os.path.join(ROOT, "packaging", "installer.iss"),
                  "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("revocations\\*", content)
        self.assertIn("[InstallDelete]", content)
        self.assertIn('Name: "{app}\\_internal"', content)
        self.assertIn('"{userdesktop}\\{#MyAppName}"', content)

    def test_preflight_prints_anchor_vcs_warning(self):
        """行为断言：preflight 必须把锚的 VCS 警告打印出来。"""
        import io
        from contextlib import redirect_stdout
        br = load_build_release()
        buf = io.StringIO()
        with mock.patch.object(br, "anchor_vcs_problems",
                               return_value=["锚未提交到 HEAD"]), \
             redirect_stdout(buf):
            br.preflight("pyinstaller", require_tools=False)
        self.assertIn("锚未提交到 HEAD", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
