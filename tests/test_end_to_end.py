"""TDD：一次完整生命周期（请求→签发→激活→到期→续期→退款撤销→回执核验）。"""
from __future__ import annotations

import os
import unittest

from licensing import (rsa_sign, fingerprint, keygen, license_core,
                       state_store)
from tests.support import LicensingTestCase


class TestFullLifecycle(LicensingTestCase):
    def test_timed_full_lifecycle(self):
        # 1. 客户机器生成激活请求
        request_text = self.request()
        parsed = fingerprint.parse_request(request_text)
        self.assertEqual(len(parsed["fp"]), 5)

        # 2. 作者签发 7 天
        token = keygen.issue("A001", request_text, "timed", 7 * 86400)
        ok, payload, _ = rsa_sign.verify_token(token, self.pub)
        self.assertTrue(ok)
        self.assertEqual(payload["seq"], 1)

        # 3. 客户导入激活
        ok, _, reason = self.activate(token)
        self.assertTrue(ok, reason)
        self.assertTrue(license_core.check_license()[0])

        # 4. 第 6 天仍可用
        self.advance(6 * 86400)
        self.assertTrue(license_core.check_license()[0])

        # 5. 第 8 天到期
        self.advance(2 * 86400)
        ok, reason = license_core.check_license()
        self.assertFalse(ok)
        self.assertIn("到期", reason)

        # 6. 续期 30 天（客户不用再发机器码）
        renewed = keygen.renew("A001", 30 * 86400)
        ok, _, reason = self.activate(renewed)
        self.assertTrue(ok, reason)
        self.assertTrue(license_core.check_license()[0])

        # 7. 退款：导入撤销令后停用
        revoke, challenge = keygen.make_revocation("A001", "用户退款")
        self.assertTrue(challenge)
        ok, receipt, reason = license_core.apply_revocation(revoke)
        self.assertTrue(ok, reason)
        ok, reason = license_core.check_license()
        self.assertFalse(ok)
        self.assertIn("作废", reason)

        # 8. 作者复算回执，确认客户确实导入过
        ok, reason = keygen.verify_receipt("A001", receipt["code"], receipt["revoked_at"])
        self.assertTrue(ok, reason)


class TestPerpetualLifecycle(LicensingTestCase):
    def test_perpetual_never_expires_and_still_revocable(self):
        token = keygen.issue("P001", self.request(), "perpetual", 0)
        ok, _, reason = self.activate(token)
        self.assertTrue(ok, reason)
        self.advance(5 * 365 * 86400)
        self.assertTrue(license_core.check_license()[0])

        revoke, _ = keygen.make_revocation("P001", "退款")
        ok, _, reason = license_core.apply_revocation(revoke)
        self.assertTrue(ok, reason)
        ok, reason = license_core.check_license()
        self.assertFalse(ok)
        self.assertIn("作废", reason)

    def test_perpetual_ignores_clock_rollback(self):
        """永久授权没有时间权益，不做回拨检查，避免改系统时间误伤买断用户。"""
        token = keygen.issue("P002", self.request(), "perpetual", 0)
        self.assertTrue(self.activate(token)[0])
        self.advance(-86400)
        ok, _, reason = license_core.validate(token)
        self.assertTrue(ok, reason)


class TestStateSurvivesAppDirRewrite(LicensingTestCase):
    def test_state_survives_app_dir_rewrite(self):
        """只验证「程序目录被替换、状态目录不动」这一层。

        真正的覆盖安装（Inno Setup 编译、AppId 固定、卸载重装）必须由人在
        干净虚拟机验收；这个用例名过去叫 UpdateKeepsLicense，名不副实。
        """
        token = self.issue()
        self.assertTrue(self.activate(token)[0])
        app_dir = os.path.join(self.tmp.name, "app")
        os.makedirs(app_dir, exist_ok=True)
        with open(os.path.join(app_dir, "game_bot.exe"), "w", encoding="utf-8") as fh:
            fh.write("v1")
        # 覆盖 exe
        with open(os.path.join(app_dir, "game_bot.exe"), "w", encoding="utf-8") as fh:
            fh.write("v2")
        self.assertTrue(license_core.check_license()[0])
        self.assertEqual(state_store.load_state()["max_seq"], 1)


if __name__ == "__main__":
    unittest.main()
