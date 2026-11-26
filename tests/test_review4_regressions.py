"""第四轮 review 回归测试（TDD 红阶段）。

覆盖第三轮两路审查判定为「暂不建议直接交付」的全部阻塞项与主要 HIGH 项：

* B-1  NTP 不可用 / 被误杀时首次激活退化为本地时间（窗口外激活 + 无限重置）
* B-2  MAX_FORWARD_SKEW 以本地时钟为基准（回拨误杀诚实 NTP、慢钟误伤客户）
* B1   状态封装密钥跨构建会变，且没有任何自动化护栏
* H1/H2 缓存过期重复同步 NTP、读-改-写竞态丢失 used_seconds
* 以及测试假绿、夹具残留线程、发布流程护栏等工程问题
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from licensing import (rsa_sign, clock, state_store,
                       license_core, keygen)
from tests.support import (LicensingTestCase, BASE_TIME,
                           isolated_release_paths)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_build_release():
    path = os.path.join(ROOT, "packaging", "build_release.py")
    spec = importlib.util.spec_from_file_location("sz_build_release_r4", path)
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


class TestActivationFailClosed(LicensingTestCase, ClockIsolatedMixin):
    """B-1：转发许可 + 阻断/误杀 NTP 不得在真实激活窗口外激活。"""

    def tearDown(self):
        self.restore_clock()
        super().tearDown()

    def test_first_activation_rejected_when_ntp_unavailable(self):
        token = self.issue(typ="timed", days=7)
        self.isolate_clock()
        # 真实时间早已超过 7 天窗口，本地回拨到窗口内 + NTP 全断
        with mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 2 * 86400), \
             mock.patch("licensing.clock._ntp_seconds", return_value=None):
            ok, _, reason = license_core.activate(token)
        self.assertFalse(ok)
        self.assertIn("联网", reason)
        self.assertIsNone(state_store.load_state())

    def test_honest_ntp_still_blocks_out_of_window_license(self):
        """诚实 NTP 报真实时间时，转发的许可必须因激活窗口过期被拒。"""
        token = self.issue(typ="timed", days=7)
        self.isolate_clock()
        with mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 200 * 86400), \
             mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 200 * 86400):
            ok, _, reason = license_core.activate(token)
        self.assertFalse(ok)
        self.assertIn("激活窗口", reason)

    def test_repeated_state_wipe_cannot_reset_timed(self):
        token = self.issue(typ="timed", days=7)
        for attempt in range(3):
            state_store.clear_state()
            self.isolate_clock()
            with mock.patch("licensing.clock.time.time",
                            return_value=BASE_TIME + 2 * 86400), \
                 mock.patch("licensing.clock._ntp_seconds", return_value=None):
                ok, payload, reason = license_core.activate(token)
            self.assertFalse(ok, "第 %d 次删状态重导入竟然成功" % attempt)
        self.assertIsNone(state_store.load_state())

    def test_first_activation_succeeds_with_ntp_consensus(self):
        token = self.issue(typ="timed", days=7)
        self.isolate_clock()
        with mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME):
            ok, _, reason = license_core.activate(token)
        self.assertTrue(ok, reason)
        self.assertIsNotNone(state_store.load_state())

    def test_explicit_now_allows_automation_without_ntp(self):
        """冒烟/单测显式注入时间时不需要再查 NTP。"""
        token = self.issue(typ="timed", days=7)
        ok, _, reason = license_core.activate(token, now=BASE_TIME)
        self.assertTrue(ok, reason)

    def test_renewal_with_existing_state_does_not_require_ntp(self):
        token = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(token)[0])
        renewed = keygen.renew("A001", 30 * 86400)
        self.isolate_clock(ntp_enabled=False)
        with mock.patch("licensing.clock.time.time", return_value=BASE_TIME + 3600):
            ok, _, reason = license_core.activate(renewed)
        self.assertTrue(ok, reason)


class TestClockNoLocalClockBaseline(LicensingTestCase, ClockIsolatedMixin):
    """B-2 / H1：本地时钟不得决定 NTP 是否被采信，也不得让主线程反复做网络 IO。"""

    def tearDown(self):
        self.restore_clock()
        super().tearDown()

    def test_local_clock_slow_40_days_honest_ntp_wins(self):
        self.isolate_clock()
        with mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME - 40 * 86400), \
             mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME):
            value = clock.refresh_now()
        self.assertAlmostEqual(value, BASE_TIME, delta=3)

    def test_local_clock_rolled_back_100_days_honest_ntp_wins(self):
        self.isolate_clock()
        with mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME - 100 * 86400), \
             mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME):
            value = clock.ntp_consensus()
        self.assertEqual(value, BASE_TIME)

    def test_ntp_consensus_requires_two_agreeing_samples(self):
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds",
                        side_effect=[BASE_TIME, None, None]):
            self.assertIsNone(clock.ntp_consensus())

    def test_single_poisoned_sample_cannot_form_majority_consensus(self):
        """控制 1/3 甚至 2/3 个源都不足以让伪造时间被采信。"""
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds",
                        side_effect=[BASE_TIME, BASE_TIME + 2 * 86400, BASE_TIME]):
            self.assertIsNone(clock.ntp_consensus())
        with mock.patch("licensing.clock._ntp_seconds",
                        side_effect=[BASE_TIME + 2 * 86400,
                                     BASE_TIME + 2 * 86400, BASE_TIME]):
            self.assertIsNone(clock.ntp_consensus())

    def test_all_available_samples_agree_when_honest(self):
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds",
                        side_effect=[BASE_TIME - 1, BASE_TIME, BASE_TIME + 1]):
            self.assertAlmostEqual(clock.ntp_consensus(), BASE_TIME, delta=1)

    def test_future_poison_rejected_relative_to_anchor_not_local(self):
        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME)
        with mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 400 * 86400):
            self.assertIsNone(clock.ntp_consensus())

    def test_small_past_ntp_repairs_anchor(self):
        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME)
        with mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME - 2 * 86400):
            self.assertEqual(clock.ntp_consensus(), BASE_TIME - 2 * 86400)

    def test_extreme_past_consensus_repairs_polluted_anchor(self):
        """被污染的远期高水位不能永久拒绝诚实 NTP（否则客户永久锁死）。"""
        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME + 40 * 86400)
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME):
            value = clock.ntp_consensus()
        self.assertEqual(value, BASE_TIME)

    def test_cache_expiry_never_triggers_sync_ntp(self):
        self.isolate_clock()
        with mock.patch("licensing.clock.ntp_consensus", return_value=None) as cons, \
             mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.monotonic", return_value=1000.0):
            clock.reset_cache()
            clock.refresh_now()
            self.assertEqual(cons.call_count, 1)
            with mock.patch("licensing.clock.time.monotonic",
                            return_value=1000.0 + 10000):
                for _ in range(5):
                    clock.trusted_now()
            self.assertEqual(cons.call_count, 1,
                             "缓存过期后 trusted_now() 又同步查了 NTP")

    def test_time_is_trusted_reflects_last_refresh(self):
        self.isolate_clock()
        # 第六轮起 refresh_now() 会做「网络时间不得大幅落后本地时钟」的
        # 交叉校验，所以这两段都必须把本地时钟一并 mock 到同一时间基准。
        with mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.time", return_value=BASE_TIME):
            clock.refresh_now()
            self.assertTrue(clock.time_is_trusted())
        self.isolate_clock()
        with mock.patch("licensing.clock._ntp_seconds", return_value=None), \
             mock.patch("licensing.clock.time.time", return_value=BASE_TIME):
            clock.refresh_now()
            self.assertFalse(clock.time_is_trusted())


class TestStateSecretFingerprintAnchor(LicensingTestCase):
    """B1：受版本控制的指纹锚 + 禁止静默生成，保证跨构建密钥不变。"""

    @unittest.skipUnless(
        os.path.isfile(os.path.join(ROOT, "licensing", "keys", "state_secret.txt")),
        "干净 checkout 无离线备份，三方一致性交给 --check 验证")
    def test_anchor_file_exists_and_matches_backup(self):
        anchor = os.path.join(ROOT, "licensing", "state_secret.fingerprint")
        backup = os.path.join(ROOT, "licensing", "keys", "state_secret.txt")
        self.assertTrue(os.path.isfile(anchor), "缺少受版本控制的密钥指纹锚")
        with open(backup, "r", encoding="utf-8") as fh:
            secret = fh.read().strip()
        with open(anchor, "r", encoding="utf-8") as fh:
            expected = fh.read().strip()
        self.assertEqual(hashlib.sha256(bytes.fromhex(secret)).hexdigest()[:32],
                         expected)

    def test_anchor_is_not_gitignored(self):
        with open(os.path.join(ROOT, ".gitignore"), "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("state_secret.fingerprint", content)

    def test_ensure_refuses_random_secret_when_anchor_exists_and_backup_missing(self):
        paths = isolated_release_paths(self.tmp.name)
        keys = os.path.join(self.tmp.name, "empty_keys")
        module = os.path.join(self.tmp.name, "missing_module.py")
        with self.assertRaises(RuntimeError):
            keygen.ensure_state_secret(
                keys, module, fingerprint_path=paths["STATE_SECRET_FINGERPRINT"])
        self.assertFalse(os.path.isfile(module))

    def test_ensure_generates_and_records_anchor_for_new_project(self):
        keys = os.path.join(self.tmp.name, "keys")
        module = os.path.join(self.tmp.name, "state_secret.py")
        anchor = os.path.join(self.tmp.name, "fingerprint.txt")
        first = keygen.ensure_state_secret(keys, module, fingerprint_path=anchor,
                                           allow_generate=True)
        self.assertTrue(os.path.isfile(anchor))
        second = keygen.ensure_state_secret(keys, module, fingerprint_path=anchor)
        self.assertEqual(first, second)
        with open(anchor, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(),
                             hashlib.sha256(bytes.fromhex(first)).hexdigest()[:32])

    def test_ensure_rejects_backup_mismatch(self):
        keys = os.path.join(self.tmp.name, "keys")
        module = os.path.join(self.tmp.name, "state_secret.py")
        anchor = os.path.join(self.tmp.name, "fingerprint.txt")
        keygen.ensure_state_secret(keys, module, fingerprint_path=anchor,
                                   allow_generate=True)
        with open(os.path.join(keys, "state_secret.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write("0" * 64)
        with self.assertRaises(ValueError):
            keygen.ensure_state_secret(keys, module, fingerprint_path=anchor)

    def test_preflight_rejects_anchor_mismatch(self):
        br = load_build_release()
        paths = isolated_release_paths(self.tmp.name)
        fake = os.path.join(self.tmp.name, "wrong.fingerprint")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write("0" * 32)
        paths["STATE_SECRET_FINGERPRINT"] = fake
        with mock.patch.multiple(br, **paths):
            problems = br.preflight("pyinstaller", require_tools=False)
        self.assertTrue(any("指纹" in p or "锚" in p for p in problems), problems)

    def test_preflight_reports_missing_backup(self):
        br = load_build_release()
        paths = isolated_release_paths(self.tmp.name)
        paths["STATE_SECRET_BACKUP"] = os.path.join(self.tmp.name, "missing.txt")
        with mock.patch.multiple(br, **paths):
            problems = br.preflight("pyinstaller", require_tools=False)
        self.assertTrue(any("备份" in p for p in problems), problems)

    def test_init_refuses_to_overwrite_existing_private_key(self):
        target = os.path.join(self.tmp.name, "keys")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "private.json"), "w",
                  encoding="utf-8") as fh:
            fh.write("{}")
        with self.assertRaises(RuntimeError):
            keygen.init_keys(target, keypair=rsa_sign.generate_keypair())


class TestAtomicStateAndSuiteHygiene(LicensingTestCase):
    def test_update_state_is_atomic_across_threads(self):
        state_store.save_state(self.base_state(used_seconds=0))
        errors = []

        def bump(state):
            state["used_seconds"] = state_store.as_int(
                state.get("used_seconds", 0)) + 1
            return state

        def worker():
            try:
                for _ in range(25):
                    state_store.update_state(bump)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(state_store.load_state()["used_seconds"], 200)

    def test_check_license_does_not_pollute_last_seen_with_untrusted_time(self):
        token = self.issue(typ="timed", days=30)
        self.assertTrue(self.activate(token)[0])
        clock.set_test_clock(None)
        clock.set_ntp_enabled(False)
        clock.set_time_anchor(None)
        clock.reset_cache()
        with mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 3600):
            ok, reason = license_core.check_license()
        self.assertTrue(ok, reason)
        self.assertEqual(state_store.load_state()["last_seen"], BASE_TIME)

    def test_state_secret_is_release_rejects_non_hex_module(self):
        with mock.patch.object(state_store, "_state_secret_module",
                               SimpleNamespace(STATE_SECRET="z" * 64)):
            self.assertFalse(state_store.state_secret_is_release())

    def test_cleanup_stops_heartbeat(self):
        license_core.stop_heartbeat()
        license_core.start_heartbeat(interval=60)
        thread = license_core._HEARTBEAT_THREAD
        self.assertIsNotNone(thread)
        self._reset_hooks()
        self.assertFalse(thread.is_alive())

    def test_run_tests_detects_leftover_license_threads(self):
        with open(os.path.join(ROOT, "tests", "run_tests.py"),
                  "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("license-", content)

    def test_cross_check_rejects_backdated_ntp(self):
        """行为断言：冒烟第 1 步依赖的方向交叉校验必须真的可能失败。"""
        self.assertFalse(clock.cross_check_ok(BASE_TIME - 30 * 86400,
                                              BASE_TIME))
        # 系统时钟慢 40 天的诚实客户：NTP 领先，必须照常采信
        self.assertTrue(clock.cross_check_ok(BASE_TIME,
                                             BASE_TIME - 40 * 86400))


class TestPackagingGuards(LicensingTestCase):
    def test_nuitka_does_not_include_whole_licensing_package(self):
        with open(os.path.join(ROOT, "packaging", "build_release.py"),
                  "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("--include-package=licensing", content)

    def test_dist_sensitive_scan_rejects_private_key_in_package(self):
        """行为断言：产物里出现私钥文件必须被扫描拦下。"""
        br = load_build_release()
        from tests.support import isolated_release_paths
        paths = isolated_release_paths(self.tmp.name)
        with mock.patch.object(br, "OUT_DIR", paths["OUT_DIR"]):
            with open(os.path.join(paths["OUT_DIR"], "private.json"), "w",
                      encoding="utf-8") as fh:
                fh.write("{}")
            self.assertTrue(br.dist_problems())

    def test_installer_files_are_whitelisted(self):
        with open(os.path.join(ROOT, "packaging", "installer.iss"),
                  "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertNotIn("ShootAtZombies\\*", content)

    def test_docs_state_activation_requires_network(self):
        with open(os.path.join(ROOT, "docs", "授权与打包方案.md"),
                  "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("首次激活必须联网", content)


if __name__ == "__main__":
    unittest.main()
