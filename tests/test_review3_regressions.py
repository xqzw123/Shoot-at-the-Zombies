"""第三轮 review 回归测试（TDD 红阶段）：时间源、畸形输入、状态密钥、并发、看门狗。

覆盖第二轮 review 判定为「暂不建议直接交付」的全部阻塞项。
"""
from __future__ import annotations

import importlib.util
import io
import os
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from licensing import (rsa_sign, fingerprint, clock, state_store,
                       license_core, keygen)
from tests.support import (LicensingTestCase, FIXED_FP, BASE_TIME, PUBLIC,
                           isolated_release_paths)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OTHER_KP = None


def other_keypair():
    global _OTHER_KP
    if _OTHER_KP is None:
        _OTHER_KP = rsa_sign.generate_keypair()
    return _OTHER_KP


def signed_payload(token: str, private: dict | None = None, **overrides):
    _, payload, _ = rsa_sign.verify_token(token, PUBLIC)
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return rsa_sign.make_token(payload, private or _kp())


def _kp():
    from tests.support import keypair
    return keypair()


def load_build_release():
    path = os.path.join(ROOT, "packaging", "build_release.py")
    spec = importlib.util.spec_from_file_location("sz_build_release_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClockIsolatedMixin:
    """把可信时间恢复成「冷缓存 + 真实逻辑」，用于模拟新进程。"""

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


class TestTimeSourceAttackSurface(LicensingTestCase, ClockIsolatedMixin):
    """核心阻塞项：NTP 不能以可被回拨的本地时钟为过滤基准，锚不能遮蔽回拨检测。"""

    def tearDown(self):
        self.restore_clock()
        super().tearDown()

    def test_honest_ntp_wins_even_when_local_clock_rolled_back_two_days(self):
        self.isolate_clock()
        with mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 2 * 86400):
            value = clock.refresh_now()
        self.assertAlmostEqual(value, BASE_TIME + 2 * 86400, delta=2)

    def test_anchor_does_not_mask_rollback_when_ntp_all_failed(self):
        """旧实现把 now 抬到 last_seen，回拨检测永不触发；这里必须拒绝。"""
        token = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(token)[0])
        self.advance(3600)
        state = state_store.load_state()
        state["last_seen"] = BASE_TIME + 3600
        state_store.save_state(state)

        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME + 3600)
        with mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 3600 - 2 * 86400), \
             mock.patch("licensing.clock._ntp_seconds", return_value=None):
            ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("回拨", reason)

    def test_freezing_local_clock_cannot_freeze_timed_expiry(self):
        """旧实现 NTP 真值被 local 偏差过滤掉，timed 永不到期。"""
        token = self.issue(typ="timed", days=1)
        self.assertTrue(self.activate(token)[0])
        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME)
        with mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 2 * 86400):
            ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("到期", reason)

    def test_all_ntp_failed_returns_raw_local_not_clamped_anchor(self):
        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME + 1000)
        with mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock._ntp_seconds", return_value=None):
            value = clock.trusted_now()
        self.assertAlmostEqual(value, BASE_TIME, delta=2)

    def test_monotonic_cache_advances_when_wall_clock_frozen(self):
        """NTP 全断且本地时钟被冻结时，单调时钟仍推进，不能把时间冻住。"""
        self.isolate_clock()
        with mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock.time.monotonic", return_value=1000.0), \
             mock.patch("licensing.clock._ntp_seconds", return_value=BASE_TIME):
            clock.reset_cache()
            clock.refresh_now()
        with mock.patch("licensing.clock.time.monotonic",
                        return_value=1000.0 + 2 * 86400), \
             mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock._ntp_seconds", return_value=None):
            value = clock.trusted_now()
        self.assertGreaterEqual(value, BASE_TIME + 2 * 86400 - 2)

    def test_used_seconds_defeats_clock_freeze(self):
        """心跳累计的真实运行时长超过日历时长时，不能让限时许可无限续命。"""
        token = self.issue(typ="timed", days=1)
        self.assertTrue(self.activate(token)[0])
        state = state_store.load_state()
        state["used_seconds"] = 2 * 86400
        state_store.save_state(state)
        ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("到期", reason)

    def test_used_seconds_over_calendar_elapsed_rejected(self):
        token = self.issue(typ="timed", days=30)
        self.assertTrue(self.activate(token)[0])
        state = state_store.load_state()
        state["used_seconds"] = 10 * 86400
        state_store.save_state(state)
        ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("时间", reason)

    def test_ntp_samples_behind_high_water_mark_repair_anchor(self):
        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME)
        with mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME - 2 * 86400):
            self.assertEqual(clock.ntp_consensus(), BASE_TIME - 2 * 86400)

    def test_implausibly_future_ntp_is_rejected(self):
        """远期投毒相对「已知可信锚」判断，绝不再用可被回拨的本地时钟。"""
        self.isolate_clock()
        clock.set_time_anchor(BASE_TIME)
        with mock.patch("licensing.clock.time.time", return_value=BASE_TIME), \
             mock.patch("licensing.clock._ntp_seconds",
                        return_value=BASE_TIME + 400 * 86400):
            self.assertIsNone(clock.ntp_consensus())

    def test_backdated_clock_cannot_extend_activation_window(self):
        """删状态后把时间调回签发前，不能靠负时间绕过 activate_before。"""
        token = self.issue(typ="timed", days=7)
        ok, _, reason = license_core.validate(
            token, state=None, now=BASE_TIME - 10 * 86400)
        self.assertFalse(ok)
        self.assertIn("签发", reason)

    def test_small_clock_skew_still_allows_activation(self):
        """作者端与客户端时钟有小时级偏差时不能误伤正常激活。"""
        token = self.issue(typ="timed", days=7)
        ok, _, reason = license_core.validate(
            token, state=None, now=BASE_TIME - 3600)
        self.assertTrue(ok, reason)

    def test_rollback_detected_on_cold_cache_after_restart(self):
        token = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(token)[0])
        self.advance(3600)
        self.isolate_clock(ntp_enabled=False)
        with mock.patch("licensing.clock.time.time",
                        return_value=BASE_TIME + 3600 - 2 * 86400):
            ok, reason = license_core.check_license()
        self.assertFalse(ok)
        self.assertIn("回拨", reason)

    def test_refresher_is_idempotent_and_stoppable(self):
        clock.set_ntp_enabled(False)
        clock.stop_time_refresher()
        clock.start_time_refresher()
        clock.start_time_refresher()
        threads = [t for t in threading.enumerate() if t.name == "license-time"]
        self.assertEqual(len(threads), 1)
        clock.stop_time_refresher()
        self.assertEqual([t for t in threading.enumerate()
                          if t.name == "license-time"], [])


class TestMalformedInputHardening(LicensingTestCase):
    def test_parse_request_rejects_dict_fp_element(self):
        data = {"v": 1, "app": fingerprint.APP_ID, "mid": "SZ-X",
                "fp": ["a" * 16, {"evil": 1}], "nonce": "n", "ts": 0}
        text = "%s.%s" % (rsa_sign.REQUEST_PREFIX,
                          rsa_sign.b64e(rsa_sign.canonical(data)))
        with self.assertRaises(ValueError):
            fingerprint.parse_request(text)

    def test_parse_request_rejects_non_hex_fp_element(self):
        data = {"v": 1, "app": fingerprint.APP_ID, "mid": "SZ-X",
                "fp": ["a" * 16, "../../etc/passwd"], "nonce": "n", "ts": 0}
        text = "%s.%s" % (rsa_sign.REQUEST_PREFIX,
                          rsa_sign.b64e(rsa_sign.canonical(data)))
        with self.assertRaises(ValueError):
            fingerprint.parse_request(text)

    def test_match_score_ignores_non_string_elements(self):
        self.assertEqual(fingerprint.match_score(FIXED_FP, ["a" * 16, {"x": 1}]), 0)

    def test_payload_with_dict_fp_element_rejected_not_crash(self):
        token = signed_payload(self.issue(), fp=[{"x": 1}, "b" * 16])
        try:
            ok, _, _ = license_core.validate(token)
        except Exception as exc:  # pragma: no cover - 失败时给出更明确的诊断
            self.fail("畸形 fp 导致未捕获异常：%r" % (exc,))
        self.assertFalse(ok)

    def test_non_finite_int_field_rejected(self):
        token = signed_payload(self.issue(), duration_seconds=float("inf"))
        ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("字段", reason)

    def test_bool_int_field_rejected(self):
        token = signed_payload(self.issue(), seq=True)
        ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("字段", reason)

    def test_foreign_private_key_license_rejected(self):
        token = signed_payload(self.issue(), other_keypair())
        ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("签名", reason)

    def test_merge_states_handles_malformed_numbers(self):
        merged = state_store.merge_states([
            self.base_state(max_seq="not-an-int", last_seen="bad"),
            self.base_state(max_seq=2, last_seen=BASE_TIME + 10),
        ])
        self.assertIsNotNone(merged)
        self.assertEqual(merged["max_seq"], 2)

    def test_malformed_state_ints_do_not_crash_check_license(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        state = state_store.load_state()
        state.update(max_seq="oops", last_seen="bad", activated_at=None,
                     used_seconds=float("inf"), revoked_at="x")
        state_store.save_state(state)
        try:
            ok, reason = license_core.check_license()
        except Exception as exc:  # pragma: no cover
            self.fail("畸形状态导致未捕获异常：%r" % (exc,))
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(reason, str)


class TestStateSecretLifecycle(LicensingTestCase):
    def test_state_secret_not_hardcoded_in_source(self):
        with open(os.path.join(ROOT, "licensing", "state_store.py"),
                  "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotRegex(source, r"_STATE_SECRET\s*=\s*bytes\.fromhex")
        self.assertNotRegex(source, r"[0-9a-f]{64}")
        self.assertIn("from licensing import state_secret", source)

    def test_state_secret_module_is_gitignored(self):
        with open(os.path.join(ROOT, ".gitignore"), "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("licensing/state_secret.py", content)

    def test_ensure_state_secret_is_random_and_stable(self):
        keys = os.path.join(self.tmp.name, "keys")
        module = os.path.join(self.tmp.name, "state_secret.py")
        anchor = os.path.join(self.tmp.name, "state_secret.fingerprint")
        first = keygen.ensure_state_secret(keys, module, fingerprint_path=anchor,
                                           allow_generate=True)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        with open(module, "r", encoding="utf-8") as fh:
            self.assertIn(first, fh.read())
        second = keygen.ensure_state_secret(keys, module, fingerprint_path=anchor)
        self.assertEqual(first, second)

    def test_state_store_secret_is_release_grade(self):
        """行为断言：注入合法构建期模块才算 release；干净 checkout 应为 False。"""
        from types import SimpleNamespace
        with mock.patch.object(state_store, "_state_secret_module",
                               SimpleNamespace(STATE_SECRET="a" * 64)):
            self.assertTrue(state_store.state_secret_is_release())
        with mock.patch.object(state_store, "_state_secret_module", None):
            self.assertFalse(state_store.state_secret_is_release())
        with mock.patch.object(state_store, "_state_secret_module",
                               SimpleNamespace(STATE_SECRET="z" * 64)):
            self.assertFalse(state_store.state_secret_is_release())

    def test_preflight_reports_missing_state_secret_module(self):
        br = load_build_release()
        paths = isolated_release_paths(self.tmp.name)
        paths["STATE_SECRET_MODULE"] = os.path.join(self.tmp.name, "missing.py")
        with mock.patch.multiple(br, **paths):
            problems = br.preflight("pyinstaller", require_tools=False)
        self.assertTrue(any("state_secret" in p for p in problems))

    def test_preflight_rejects_wrong_public_fingerprint(self):
        from licensing import public_key
        br = load_build_release()
        paths = isolated_release_paths(self.tmp.name)
        with mock.patch.object(public_key, "FINGERPRINT", "0000000000000000"), \
             mock.patch.multiple(br, **paths):
            problems = br.preflight("pyinstaller", require_tools=False)
        self.assertTrue(any("FINGERPRINT" in p or "指纹" in p for p in problems))
        self.assertTrue(all("状态封装密钥" not in p for p in problems),
                        "隔离后不应有无关的状态密钥问题掺入：%s" % problems)

    def test_check_requires_tooling_and_does_not_claim_unchecked_tools(self):
        """行为断言：--check 仍要求引擎工具可用，成功文案不谎称已验证工具。"""
        br = load_build_release()
        buf = io.StringIO()
        with mock.patch.object(br, "preflight", return_value=[]) as preflight, \
             mock.patch.object(br.os.path, "isfile", return_value=False), \
             redirect_stdout(buf):
            rc = br.main(["--check"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertNotIn("打包工具", buf.getvalue())
        self.assertTrue(preflight.call_args.kwargs.get("require_tools"),
                        "--check 漏掉了引擎工具可用性检查")


class TestConcurrencyAndWatchdog(LicensingTestCase):
    def test_temp_paths_are_unique(self):
        paths = {state_store._temp_path(state_store.state_path()) for _ in range(30)}
        self.assertEqual(len(paths), 30)

    def test_concurrent_saves_do_not_corrupt_state(self):
        errors = []

        def writer(index):
            try:
                for _ in range(10):
                    state_store.save_state(self.base_state(max_seq=index))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(1, 9)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertIsNotNone(state_store.load_state())
        leftovers = [name for name in os.listdir(self.tmp.name)
                     if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_shutdown_calls_gui_quit_app_before_root_quit(self):
        import launcher
        root = mock.Mock()
        gui = mock.Mock()
        launcher._shutdown(root, gui)
        gui.quit_app.assert_called_once_with()
        root.quit.assert_not_called()

    def test_shutdown_falls_back_to_root_quit_without_gui(self):
        import launcher
        root = mock.Mock()
        launcher._shutdown(root, None)
        root.quit.assert_called_once_with()

    def test_shutdown_survives_broken_gui(self):
        import launcher
        root = mock.Mock()
        gui = mock.Mock()
        gui.quit_app.side_effect = RuntimeError("gui broken")
        launcher._shutdown(root, gui)
        root.quit.assert_called_once_with()

    def test_watchdog_exits_only_after_three_consecutive_failures(self):
        import launcher
        scheduled = []
        root = mock.Mock()
        root.after.side_effect = lambda delay, fn: scheduled.append((delay, fn))
        gui = mock.Mock()
        with mock.patch.object(launcher.license_core, "check_license",
                               return_value=(False, "到期")):
            watch = launcher._make_watchdog(root, gui)
            for _ in range(2):
                watch()
        gui.quit_app.assert_not_called()
        root.quit.assert_not_called()
        with mock.patch.object(launcher.license_core, "check_license",
                               return_value=(False, "到期")):
            watch()
        gui.quit_app.assert_called_once_with()
        self.assertEqual(len(scheduled), 2)

    def test_stop_heartbeat_joins_worker(self):
        license_core.stop_heartbeat()
        license_core.start_heartbeat(interval=60)
        thread = license_core._HEARTBEAT_THREAD
        self.assertIsNotNone(thread)
        license_core.stop_heartbeat()
        self.assertFalse(thread.is_alive())


class TestReceiptWindowContinues(LicensingTestCase):
    def test_verify_receipt_continues_past_out_of_window_record(self):
        """同一 challenge 补发两次时，第一条超窗口不能提前 return False。"""
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        machine = fingerprint.machine_code()
        with mock.patch("licensing.keygen.secrets.token_hex", return_value="c" * 16):
            keygen.make_revocation("A001", "首次")
            self.advance(100 * 86400)
            keygen.make_revocation("A001", "补发")
        at = clock.trusted_now()
        code = license_core.receipt_code("c" * 16, machine, at)
        ok, reason = keygen.verify_receipt("A001", code, at)
        self.assertTrue(ok, reason)


class TestPublicKeySelfCheckIsReal(LicensingTestCase):
    def test_override_does_not_make_self_check_vacuously_true(self):
        from licensing import public_key
        license_core.set_public_key({"n": 12345, "e": 65537})
        with mock.patch.object(public_key, "FINGERPRINT", "deadbeefdeadbeef"):
            self.assertFalse(license_core.public_key_is_consistent())

    def test_real_builtin_public_key_passes_even_with_override(self):
        license_core.set_public_key({"n": 12345, "e": 65537})
        self.assertTrue(license_core.public_key_is_consistent())


class TestRepositoryNotes(LicensingTestCase):
    def test_run_bat_marks_dev_entry_without_license(self):
        with open(os.path.join(ROOT, "run.bat"), "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("DEPRECATED", content)


if __name__ == "__main__":
    unittest.main()
