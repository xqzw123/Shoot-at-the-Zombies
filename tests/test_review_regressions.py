"""第二轮 review 回归测试：先把发现的问题写成用例，再修实现（TDD）。"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

from licensing import (rsa_sign, fingerprint, clock, state_store,
                       license_core, keygen)
from tests.support import LicensingTestCase, OTHER_FP, BASE_TIME, PUBLIC

_OTHER_KP = None


def other_keypair():
    global _OTHER_KP
    if _OTHER_KP is None:
        _OTHER_KP = rsa_sign.generate_keypair()
    return _OTHER_KP


def payload_like(token: str, **overrides):
    _, payload, _ = rsa_sign.verify_token(token, PUBLIC)
    for key, value in overrides.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


class FakeWinReg:
    """最小 winreg 替身，避免测试碰真实注册表。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_WRITE = 1
    KEY_READ = 2
    REG_SZ = 1

    class _Key:
        def __init__(self, owner, path):
            self.owner = owner
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def __init__(self):
        self.store = {}

    def CreateKeyEx(self, hkey, path, reserved, access):
        return self._Key(self, path)

    def OpenKey(self, hkey, path, reserved, access):
        if path not in self.store:
            raise OSError("key missing")
        return self._Key(self, path)

    def SetValueEx(self, key, name, reserved, value_type, value):
        self.store[key.path] = value

    def QueryValueEx(self, key, name):
        return (self.store[key.path], self.REG_SZ)

    def DeleteKey(self, hkey, path):
        self.store.pop(path, None)


class TestCleanupSafety(LicensingTestCase):
    def test_reset_hooks_never_clears_real_state(self):
        """cleanup 不得再调用 clear_state，否则会删真实 APPDATA / HKCU。"""
        with mock.patch("licensing.state_store.clear_state") as cleared:
            self._reset_hooks()
            cleared.assert_not_called()


class TestSealedState(LicensingTestCase):
    def test_state_blob_has_no_plaintext_fields(self):
        state_store.save_state(self.base_state())
        with open(os.path.join(self.tmp.name, state_store.STATE_FILENAME),
                  "r", encoding="utf-8") as fh:
            raw = fh.read()
        for word in ("max_seq", "activated_at", "revoked", "token"):
            self.assertNotIn(word, raw)

    def test_tampered_sealed_state_rejected(self):
        state_store.save_state(self.base_state())
        path = os.path.join(self.tmp.name, state_store.STATE_FILENAME)
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        blob = data["sealed"]
        data["sealed"] = ("A" if blob[0] != "A" else "B") + blob[1:]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertIsNone(state_store.load_state())

    def test_merge_takes_token_from_highest_seq(self):
        a = self.base_state(max_seq=2, token="TOKEN_SEQ2", last_seen=100)
        b = self.base_state(max_seq=1, token="TOKEN_SEQ1", last_seen=200)
        merged = state_store.merge_states([a, b])
        self.assertEqual(merged["token"], "TOKEN_SEQ2")
        self.assertEqual(merged["max_seq"], 2)


class TestPayloadSchema(LicensingTestCase):
    def _signed(self, **overrides):
        payload = payload_like(self.issue(), **overrides)
        return rsa_sign.make_token(payload, self.kp)

    def test_missing_order_rejected_not_crash(self):
        ok, _, reason = license_core.validate(self._signed(order=None))
        self.assertFalse(ok)
        self.assertIn("字段", reason)

    def test_min_match_zero_cannot_bypass_machine_binding(self):
        token = self._signed(min_match=0)
        fingerprint.set_test_components(list(OTHER_FP))
        ok, _, _ = license_core.validate(token)
        self.assertFalse(ok)

    def test_bad_duration_type_rejected(self):
        ok, _, reason = license_core.validate(self._signed(duration_seconds="abc"))
        self.assertFalse(ok)
        self.assertIn("字段", reason)


class TestForeignKey(LicensingTestCase):
    def test_revocation_signed_by_other_private_key_rejected(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        payload = payload_like(token, type="revoke", target_order="A001",
                               challenge="fake", reason="退款")
        forged = rsa_sign.make_token(payload, other_keypair())
        self.assertFalse(license_core.apply_revocation(forged)[0])
        self.assertTrue(license_core.validate(token)[0])


class TestClockHardening(LicensingTestCase):
    def setUp(self):
        super().setUp()
        clock.set_test_clock(None)
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()

    def tearDown(self):
        clock.stop_time_refresher()
        clock.reset_cache()
        super().tearDown()

    def test_ntp_true_median(self):
        # 采样不再以可被回拨的 local 为过滤基准；三源取真中位数。
        # 第六轮把「极差 ≤1 天」收紧为「每样本与中位数 ≤30 秒」，
        # 因此这里用秒级抖动的真实样本，而不是 100 秒跨度的假样本。
        with mock.patch("licensing.clock.time.time", return_value=200), \
             mock.patch("licensing.clock._ntp_seconds", side_effect=[199, 200, 201]):
            self.assertEqual(clock.ntp_consensus(), 200)

    def test_past_ntp_samples_repair_polluted_high_water_mark(self):
        # 多源一致且落后于锚：判定为「历史锚被调快的本地时钟污染」，下调锚并
        # 采信诚实 NTP。旧实现只在落后 >30 天时自愈，10 天内会永久锁死客户。
        clock.set_time_anchor(BASE_TIME)
        with mock.patch("licensing.clock._ntp_seconds",
                        side_effect=[BASE_TIME - 2 * 86400,
                                     BASE_TIME - 2 * 86400 + 1, None]):
            self.assertEqual(clock.ntp_consensus(), BASE_TIME - 2 * 86400)

    def test_all_ntp_failed_returns_raw_local(self):
        # 不再把 now 抬到锚：抬锚会架空 validate() 的回拨检测
        with mock.patch("licensing.clock.time.time", return_value=1000), \
             mock.patch("licensing.clock._ntp_seconds", return_value=None):
            self.assertAlmostEqual(clock.refresh_now(), 1000, delta=2)

    def test_ntp_reply_sanity_rejects_bad_stratum(self):
        self.assertIsNone(clock.parse_ntp_reply(b"\x00" * 48))


class TestRegistryRoundTrip(LicensingTestCase):
    def test_registry_write_read_roundtrip(self):
        fake = FakeWinReg()
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            state_store.set_registry_enabled(True)
            state_store.save_state(self.base_state(max_seq=7))
            loaded = state_store.load_state()
            self.assertEqual(loaded["max_seq"], 7)

    def test_corrupt_file_falls_back_to_registry(self):
        fake = FakeWinReg()
        with mock.patch.dict(sys.modules, {"winreg": fake}):
            state_store.set_registry_enabled(True)
            state_store.save_state(self.base_state(max_seq=9))
            path = os.path.join(self.tmp.name, state_store.STATE_FILENAME)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{broken")
            loaded = state_store.load_state()
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["max_seq"], 9)

    def test_powershell_failure_returns_empty(self):
        with mock.patch("licensing.fingerprint.subprocess.check_output",
                        side_effect=OSError("powershell unavailable")):
            self.assertEqual(fingerprint._bios_uuid(), "")


class TestReceiptHardening(LicensingTestCase):
    def test_first_revocation_receipt_still_verifiable(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        first, _ = keygen.make_revocation("A001", "退款")
        _, receipt, _ = license_core.apply_revocation(first)
        keygen.make_revocation("A001", "补发")
        ok, reason = keygen.verify_receipt("A001", receipt["code"],
                                           receipt["revoked_at"])
        self.assertTrue(ok, reason)

    def test_receipt_outside_window_rejected(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        revoke, _ = keygen.make_revocation("A001", "退款")
        _, receipt, _ = license_core.apply_revocation(revoke)
        ok, _ = keygen.verify_receipt("A001", receipt["code"],
                                      receipt["revoked_at"] + 400 * 86400)
        self.assertFalse(ok)


class TestRepositoryGuards(LicensingTestCase):
    def test_legacy_build_bat_warns(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "build_exe.bat"), "r", encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("DEPRECATED", content)

    def test_public_key_fingerprint_self_check(self):
        # 自检始终校验随包发布的内置公钥；清掉注入是为了让断言覆盖真实模块
        license_core.set_public_key(None)
        from licensing import public_key
        self.assertTrue(license_core.public_key_is_consistent())
        with mock.patch.object(public_key, "FINGERPRINT", "deadbeefdeadbeef"):
            self.assertFalse(license_core.public_key_is_consistent())


if __name__ == "__main__":
    unittest.main()
