"""TDD：许可证校验、激活、seq 防重放、时间防回拨、撤销令与回执。"""
from __future__ import annotations

import os
import unittest

from licensing import rsa_sign, fingerprint, state_store, license_core, keygen, clock
from tests.support import LicensingTestCase, FIXED_FP, OTHER_FP, BASE_TIME, PUBLIC


class TestValidate(LicensingTestCase):
    def test_perpetual_ok(self):
        token = self.issue(typ="perpetual", days=0)
        ok, _, reason = license_core.validate(token)
        self.assertTrue(ok, reason)

    def test_perpetual_ignores_time(self):
        token = self.issue(typ="perpetual", days=0)
        self.assertTrue(self.activate(token)[0])
        self.advance(365 * 86400)
        ok, _, reason = license_core.validate(token)
        self.assertTrue(ok, reason)

    def test_expiry_text_is_activation_plus_duration(self):
        import time
        token = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(token)[0])
        expected = time.strftime("%Y-%m-%d %H:%M", time.localtime(BASE_TIME + 7 * 86400))
        self.assertEqual(license_core.expiry_text(), "到期 %s" % expected)

    def test_expiry_text_perpetual(self):
        token = self.issue(typ="perpetual", days=0)
        self.assertTrue(self.activate(token)[0])
        self.assertEqual(license_core.expiry_text(), "永久授权")

    def test_timed_ok_then_expired(self):
        token = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(token)[0])
        self.advance(6 * 86400)
        self.assertTrue(license_core.validate(token)[0])
        self.advance(2 * 86400)
        ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("到期", reason)

    def test_fixed_seconds_duration(self):
        token = self.issue(typ="timed", days=30)
        ok, payload, _ = rsa_sign.verify_token(token, PUBLIC)
        self.assertTrue(ok)
        self.assertEqual(payload["duration_seconds"], 30 * 86400)

    def test_activation_window_expired(self):
        token = self.issue(typ="timed", days=7)
        self.advance(8 * 86400)
        ok, _, reason = self.activate(token)
        self.assertFalse(ok)
        self.assertIn("激活", reason)

    def test_machine_mismatch_rejected(self):
        token = self.issue()
        fingerprint.set_test_components(list(OTHER_FP))
        ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("机器", reason)

    def test_one_hardware_change_tolerated(self):
        token = self.issue()
        fingerprint.set_test_components(FIXED_FP[:-1])
        ok, _, reason = license_core.validate(token)
        self.assertTrue(ok, reason)

    def test_signature_tamper_rejected(self):
        token = self.issue()
        head, body, sig = token.split(".")
        # 确定性篡改：不能写成 "A"+sig[1:]，原串首字符恰好是 A 时测试会假绿
        bad_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
        ok, _, _ = license_core.validate("%s.%s.%s" % (head, body, bad_sig))
        self.assertFalse(ok)

    def test_foreign_private_key_rejected(self):
        from tests.support import keypair
        _, payload, _ = rsa_sign.verify_token(self.issue(), PUBLIC)
        other = rsa_sign.generate_keypair()
        self.assertNotEqual(other["n"], keypair()["n"])
        forged = rsa_sign.make_token(payload, other)
        ok, _, reason = license_core.validate(forged)
        self.assertFalse(ok)
        self.assertIn("签名", reason)

    def test_revoke_token_not_accepted_as_license(self):
        token, _ = keygen.make_revocation("A001", "退款")
        ok, _, _ = license_core.validate(token)
        self.assertFalse(ok)

    def test_unknown_type_rejected(self):
        payload = self._payload_with(type="magic")
        ok, _, _ = license_core.validate(rsa_sign.make_token(payload, self.kp))
        self.assertFalse(ok)

    def _payload_with(self, **overrides):
        token = self.issue()
        ok, payload, _ = rsa_sign.verify_token(token, PUBLIC)
        self.assertTrue(ok)
        payload.update(overrides)
        return payload


class TestActivation(LicensingTestCase):
    def test_activate_writes_state(self):
        token = self.issue()
        ok, _, reason = self.activate(token)
        self.assertTrue(ok, reason)
        state = state_store.load_state()
        self.assertEqual(state["order"], "A001")
        self.assertEqual(state["activated_at"], BASE_TIME)
        self.assertEqual(state["max_seq"], 1)
        self.assertFalse(state["revoked"])

    def test_old_seq_rejected(self):
        first = self.issue(typ="timed", days=1)
        self.assertTrue(self.activate(first)[0])
        self.advance(2 * 86400)  # 到期后才允许续期
        newer = keygen.renew("A001", 30 * 86400)
        self.assertTrue(self.activate(newer)[0])
        ok, _, reason = self.activate(first)
        self.assertFalse(ok)
        self.assertTrue(any(k in reason for k in ("过期", "作废", "旧")), reason)


    def test_clock_rollback_rejected(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        self.advance(3600)
        self.assertTrue(license_core.validate(token)[0])
        self.advance(-7200)
        ok, _, reason = license_core.validate(token)
        self.assertFalse(ok)
        self.assertIn("时间", reason)

    def test_delete_state_then_reimport_old_license_fails(self):
        """删状态重来不会延长，反而因为激活窗口已过而自锁。"""
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        self.advance(10 * 86400)
        state_store.clear_state()
        ok, _, reason = self.activate(token)
        self.assertFalse(ok)
        self.assertIn("激活", reason)

    def test_stack_adds_remaining_seconds(self):
        """有效期内导入新码：剩余 + 新时长。"""
        first = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(first)[0])
        self.advance(6 * 86400)  # 还剩 1 天
        newer = keygen.renew("A001", 7 * 86400)
        ok, _, reason = self.activate(newer)
        self.assertTrue(ok, reason)
        state = state_store.load_state()
        self.assertEqual(state["activated_at"], BASE_TIME + 6 * 86400)
        self.assertEqual(state["bonus_seconds"], 1 * 86400)
        # 剩余 1 天 + 新 7 天 = 8 天
        self.advance(8 * 86400 - 1)
        self.assertTrue(license_core.check_license()[0])
        self.advance(2)
        self.assertFalse(license_core.check_license()[0])

    def test_same_token_rejected_on_reimport(self):
        token = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(token)[0])
        ok, _, reason = self.activate(token)
        self.assertFalse(ok)
        self.assertIn("重复", reason)


    def test_renew_after_expiry_resets_without_bonus(self):
        token = self.issue(typ="timed", days=7)
        self.assertTrue(self.activate(token)[0])
        self.advance(8 * 86400)
        newer = keygen.renew("A001", 30 * 86400)
        ok, _, reason = self.activate(newer)
        self.assertTrue(ok, reason)
        state = state_store.load_state()
        self.assertEqual(state["activated_at"], BASE_TIME + 8 * 86400)
        self.assertEqual(state.get("bonus_seconds", 0), 0)


    def test_check_license_uses_stored_token(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        ok, reason = license_core.check_license()
        self.assertTrue(ok, reason)


class TestRevocation(LicensingTestCase):
    def test_revocation_by_machine_code_ignores_stored_order(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        machine = fingerprint.machine_code()
        self.assertNotEqual(state_store.load_state()["order"], machine)
        revoke, _ = keygen.make_revocation(machine, "取消授权")
        ok, _, reason = license_core.apply_revocation(revoke)
        self.assertTrue(ok, reason)
        self.assertFalse(license_core.check_license()[0])

    def test_revocation_matching_order(self):
        token = self.issue(order="A001")
        self.assertTrue(self.activate(token)[0])
        revoke, _ = keygen.make_revocation("A001", "退款")
        ok, receipt, reason = license_core.apply_revocation(revoke)
        self.assertTrue(ok, reason)
        self.assertEqual(receipt["order"], "A001")
        self.assertTrue(receipt["code"])
        self.assertFalse(license_core.validate(token)[0])

    def test_revocation_does_not_hurt_other_order(self):
        token = self.issue(order="A001")
        self.assertTrue(self.activate(token)[0])
        revoke, _ = keygen.make_revocation("A002", "退款")
        ok, _, reason = license_core.apply_revocation(revoke)
        self.assertFalse(ok)
        self.assertTrue(license_core.validate(token)[0])

    def test_revocation_persists_after_token_file_deleted(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        revoke, _ = keygen.make_revocation("A001", "退款")
        self.assertTrue(license_core.apply_revocation(revoke)[0])
        folder = os.path.join(self.tmp.name, "revocations")
        os.makedirs(folder, exist_ok=True)
        for name in os.listdir(folder):
            os.remove(os.path.join(folder, name))
        self.assertFalse(license_core.validate(token)[0])

    def test_receipt_verified_by_keygen(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        revoke, _ = keygen.make_revocation("A001", "退款")
        _, receipt, _ = license_core.apply_revocation(revoke)
        ok, reason = keygen.verify_receipt("A001", receipt["code"], receipt["revoked_at"])
        self.assertTrue(ok, reason)

    def test_wrong_receipt_rejected(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        keygen.make_revocation("A001", "退款")
        ok, _ = keygen.verify_receipt("A001", "DEADBEEFDEADBEEF", BASE_TIME)
        self.assertFalse(ok)

    def test_revocation_scan_on_startup(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        revoke, _ = keygen.make_revocation("A001", "退款")
        folder = os.path.join(self.tmp.name, "revocations")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "A001.revoke"), "w", encoding="utf-8") as fh:
            fh.write(revoke)
        self.assertEqual(license_core.scan_revocations(), 1)
        self.assertFalse(license_core.check_license()[0])

    def test_forged_revocation_rejected(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        _, payload, _ = rsa_sign.verify_token(token, PUBLIC)
        payload.update(type="revoke", target_order="A001", challenge="fake")
        forged = rsa_sign.make_token(payload, self.kp)
        # 篡改签名，验证伪造凭证不会被应用。
        head, body, sig = forged.split(".")
        bad_sig = ("A" if sig[0] != "A" else "B") + sig[1:]
        tampered = "%s.%s.%s" % (head, body, bad_sig)
        self.assertFalse(license_core.apply_revocation(tampered)[0])
        self.assertTrue(license_core.validate(token)[0])


class TestClock(LicensingTestCase):
    def test_test_clock_used(self):
        self.assertEqual(clock.trusted_now(), BASE_TIME)

    def test_advance(self):
        self.advance(60)
        self.assertEqual(clock.trusted_now(), BASE_TIME + 60)


if __name__ == "__main__":
    unittest.main()
