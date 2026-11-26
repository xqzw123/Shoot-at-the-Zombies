"""TDD：作者端签发器 CLI、台账、续期、撤销令与回执复算。"""
from __future__ import annotations

import os
import unittest

from licensing import rsa_sign, keygen, license_core
from tests.support import LicensingTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestKeygen(LicensingTestCase):
    def test_issue_writes_ledger(self):
        self.issue()
        rows = keygen.read_ledger()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order"], "A001")
        self.assertEqual(rows[0]["seq"], 1)
        self.assertTrue(rows[0]["request_text"])

    def test_duplicate_initial_issue_rejected(self):
        self.issue()
        with self.assertRaises(ValueError):
            self.issue(order="A001", typ="perpetual", days=0)

    def test_renew_increments_seq_and_keeps_machine(self):
        first = self.issue()
        _, first_payload, _ = rsa_sign.verify_token(first, self.pub)
        newer = keygen.renew("A001", 30 * 86400)
        ok, payload, reason = rsa_sign.verify_token(newer, self.pub)
        self.assertTrue(ok, reason)
        self.assertEqual(payload["seq"], 2)
        self.assertEqual(payload["duration_seconds"], 30 * 86400)
        self.assertEqual(payload["fp"], first_payload["fp"])
        self.assertEqual(payload["mid"], first_payload["mid"])

    def test_renew_unknown_order_fails(self):
        with self.assertRaises(ValueError):
            keygen.renew("NO-SUCH-ORDER", 86400)

    def test_revoke_records_challenge(self):
        self.issue()
        token, challenge = keygen.make_revocation("A001", "退款")
        ok, payload, reason = rsa_sign.verify_token(token, self.pub)
        self.assertTrue(ok, reason)
        self.assertEqual(payload["type"], "revoke")
        self.assertEqual(payload["target_order"], "A001")
        self.assertEqual(payload["challenge"], challenge)
        self.assertTrue(challenge)

    def test_revocation_does_not_consume_seq(self):
        self.issue()
        keygen.make_revocation("A001", "退款")
        newer = keygen.renew("A001", 7 * 86400)
        _, payload, _ = rsa_sign.verify_token(newer, self.pub)
        self.assertEqual(payload["seq"], 2)

    def test_init_keys_writes_private_and_public(self):
        target = os.path.join(self.tmp.name, "fresh-keys")
        keygen.init_keys(target, keypair=self.kp)
        self.assertTrue(os.path.isfile(os.path.join(target, "private.json")))
        self.assertTrue(os.path.isfile(os.path.join(target, "public_key.py")))
        with open(os.path.join(target, "public_key.py"), "r", encoding="utf-8") as fh:
            self.assertIn("N =", fh.read())

    def test_cli_sign(self):
        request_path = os.path.join(self.tmp.name, "req.txt")
        with open(request_path, "w", encoding="utf-8") as fh:
            fh.write(self.request())
        out_path = os.path.join(self.tmp.name, "A002.lic")
        rc = keygen.main(["sign", "--request-file", request_path, "--type", "timed",
                          "--days", "7", "--order", "A002", "--out", out_path])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(out_path))
        with open(out_path, "r", encoding="utf-8") as fh:
            ok, payload, reason = rsa_sign.verify_token(fh.read(), self.pub)
        self.assertTrue(ok, reason)
        self.assertEqual(payload["order"], "A002")

    def test_cli_verify_receipt(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        revoke, _ = keygen.make_revocation("A001", "退款")
        _, receipt, _ = license_core.apply_revocation(revoke)
        rc = keygen.main(["verify-receipt", "--order", "A001", "--code",
                          receipt["code"], "--at", str(receipt["revoked_at"])])
        self.assertEqual(rc, 0)

    def test_cli_list(self):
        self.issue()
        self.assertEqual(keygen.main(["list"]), 0)

    def test_gui_entrypoint_exists(self):
        """结构断言：用 AST 检查 GUI 入口与四个签发动作，不依赖 tkinter 可导入。"""
        import ast
        with open(os.path.join(ROOT, "licensing", "keygen_ui.py"),
                  "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        functions = {n.name for n in tree.body
                     if isinstance(n, ast.FunctionDef)}
        classes = {n.name: {m.name for m in n.body
                            if isinstance(m, ast.FunctionDef)}
                   for n in tree.body if isinstance(n, ast.ClassDef)}
        self.assertIn("launch", functions)
        self.assertIn("KeygenWindow", classes)
        self.assertTrue({"do_sign", "do_renew", "do_revoke", "do_verify"}
                        <= classes["KeygenWindow"])


class TestLedgerIsolation(LicensingTestCase):
    def test_ledger_path_is_isolated(self):
        self.issue()
        path = keygen.ledger_path()
        self.assertEqual(os.path.dirname(path), self.tmp.name)
        self.assertTrue(os.path.isfile(path))

    def test_state_does_not_live_in_ledger_dir_as_plaintext_token(self):
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        with open(keygen.ledger_path(), "r", encoding="utf-8") as fh:
            self.assertNotIn(token, fh.read())


if __name__ == "__main__":
    unittest.main()
