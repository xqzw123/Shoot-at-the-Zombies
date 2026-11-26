"""TDD：RSA 签名、许可证编解码、机器指纹、状态存储。"""
from __future__ import annotations

import json
import os
import unittest

from licensing import rsa_sign, fingerprint, state_store
from tests.support import (LicensingTestCase, keypair, PUBLIC, FIXED_FP,
                           OTHER_FP)


class TestRsaSign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kp = keypair()
        cls.pub = PUBLIC

    def test_sign_verify_roundtrip(self):
        msg = rsa_sign.canonical({"hello": "world"})
        self.assertTrue(rsa_sign.verify(msg, rsa_sign.sign(msg, self.kp), self.pub))

    def test_tampered_message_rejected(self):
        sig = rsa_sign.sign(rsa_sign.canonical({"a": 1}), self.kp)
        self.assertFalse(rsa_sign.verify(rsa_sign.canonical({"a": 2}), sig, self.pub))

    def test_tampered_signature_rejected(self):
        msg = rsa_sign.canonical({"a": 1})
        sig = bytearray(rsa_sign.sign(msg, self.kp))
        sig[-1] ^= 1
        self.assertFalse(rsa_sign.verify(msg, bytes(sig), self.pub))

    def test_other_public_key_rejected(self):
        msg = rsa_sign.canonical({"a": 1})
        sig = rsa_sign.sign(msg, self.kp)
        self.assertFalse(rsa_sign.verify(msg, sig, {"n": self.pub["n"] + 2, "e": 65537}))

    def test_short_key_rejected(self):
        with self.assertRaises(ValueError):
            rsa_sign.generate_keypair(1024)

    def test_token_roundtrip(self):
        payload = {"v": 1, "order": "A001", "seq": 1}
        ok, parsed, reason = rsa_sign.verify_token(rsa_sign.make_token(payload, self.kp), self.pub)
        self.assertTrue(ok, reason)
        self.assertEqual(parsed, payload)

    def test_token_payload_tamper_rejected(self):
        token = rsa_sign.make_token({"v": 1, "order": "A001"}, self.kp)
        head, body, sig = token.split(".")
        i = len(body) // 2
        bad = body[:i] + ("A" if body[i] != "A" else "B") + body[i + 1:]
        ok, _, _ = rsa_sign.verify_token("%s.%s.%s" % (head, bad, sig), self.pub)
        self.assertFalse(ok)

    def test_token_whitespace_tolerated(self):
        token = rsa_sign.make_token({"a": 1}, self.kp)
        ok, parsed, reason = rsa_sign.verify_token(rsa_sign.split_lines(token, 40), self.pub)
        self.assertTrue(ok, reason)
        self.assertEqual(parsed, {"a": 1})

    def test_malformed_token_rejected(self):
        ok, _, _ = rsa_sign.verify_token("not-a-token", self.pub)
        self.assertFalse(ok)

    def test_canonical_is_stable(self):
        self.assertEqual(rsa_sign.canonical({"b": 2, "a": 1}),
                         rsa_sign.canonical({"a": 1, "b": 2}))

    def test_public_fingerprint_stable(self):
        a = rsa_sign.public_fingerprint(self.pub)
        self.assertEqual(a, rsa_sign.public_fingerprint(dict(self.pub)))
        self.assertEqual(len(a), 16)


class TestFingerprint(LicensingTestCase):
    def test_components_injected(self):
        self.assertEqual(fingerprint.components(), FIXED_FP)

    def test_machine_code_stable_and_formatted(self):
        code = fingerprint.machine_code()
        self.assertEqual(code, fingerprint.machine_code())
        self.assertTrue(code.startswith("SZ-"))
        self.assertLessEqual(len(code), 32)

    def test_request_roundtrip(self):
        req = fingerprint.make_request()
        self.assertTrue(req.startswith(rsa_sign.REQUEST_PREFIX))
        data = fingerprint.parse_request(req)
        self.assertEqual(data["fp"], FIXED_FP)
        self.assertEqual(data["v"], 1)
        self.assertTrue(data["nonce"])

    def test_request_whitespace_tolerated(self):
        data = fingerprint.parse_request(rsa_sign.split_lines(fingerprint.make_request(), 30))
        self.assertEqual(data["fp"], FIXED_FP)

    def test_request_rejects_garbage(self):
        with self.assertRaises(ValueError):
            fingerprint.parse_request("hello")

    def test_too_few_components_rejected(self):
        fingerprint.set_test_components(["only-one"])
        with self.assertRaises(ValueError):
            fingerprint.make_request()

    def test_match_score(self):
        self.assertEqual(fingerprint.match_score(FIXED_FP, FIXED_FP), 5)
        self.assertEqual(fingerprint.match_score(FIXED_FP, FIXED_FP[:2]), 2)
        self.assertEqual(fingerprint.match_score(FIXED_FP, OTHER_FP), 0)

    def test_min_match_for(self):
        self.assertEqual(fingerprint.min_match_for(5), 3)
        self.assertEqual(fingerprint.min_match_for(3), 3)
        self.assertEqual(fingerprint.min_match_for(2), 2)

    def test_hardware_change_tolerated(self):
        self.assertGreaterEqual(fingerprint.match_score(FIXED_FP, FIXED_FP[:-1]),
                                fingerprint.min_match_for(5))


class TestStateStore(LicensingTestCase):
    def test_save_load_roundtrip(self):
        state_store.save_state(self.base_state(order="A001", max_seq=3))
        loaded = state_store.load_state()
        self.assertEqual(loaded["order"], "A001")
        self.assertEqual(loaded["max_seq"], 3)

    def test_tampered_state_rejected(self):
        state_store.save_state(self.base_state())
        path = os.path.join(self.tmp.name, state_store.STATE_FILENAME)
        # 状态是密文封装，换成任意垃圾内容必须被拒绝
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"v": 1, "sealed": "not-a-valid-blob"}, fh)
        self.assertIsNone(state_store.load_state())

    def test_merge_takes_stricter_values(self):
        a = self.base_state(activated_at=100, last_seen=200, max_seq=1,
                            used_seconds=10, revoked=False)
        b = self.base_state(activated_at=120, last_seen=300, max_seq=2,
                            used_seconds=20, revoked=True)
        merged = state_store.merge_states([a, b])
        self.assertEqual(merged["activated_at"], 100)
        self.assertEqual(merged["last_seen"], 300)
        self.assertEqual(merged["max_seq"], 2)
        self.assertEqual(merged["used_seconds"], 20)
        self.assertTrue(merged["revoked"])

    def test_merge_ignores_invalid_states(self):
        merged = state_store.merge_states([None, {}, self.base_state()])
        self.assertEqual(merged["order"], "A001")

    def test_clear_state(self):
        state_store.save_state(self.base_state())
        state_store.clear_state()
        self.assertIsNone(state_store.load_state())


if __name__ == "__main__":
    unittest.main()
