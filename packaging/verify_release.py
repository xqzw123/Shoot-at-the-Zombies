"""发布前真实流程冒烟验证：python packaging/verify_release.py

用生产密钥、真实机器指纹跑一遍完整的签发→激活→撤销→回执闭环，
但状态与台账全部隔离在临时目录，不碰真实 APPDATA / 注册表 / 台账。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from licensing import (rsa_sign, fingerprint, clock, state_store,
                       license_core, keygen)


def step(label: str, ok: bool, detail: str = "", skipped: bool = False) -> None:
    tag = "[SKIP]" if skipped else ("[OK]" if ok else "[FAIL]")
    print("%-6s %-28s %s" % (tag, label, detail))
    if not ok:
        raise SystemExit(1)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="szl-smoke-") as tmp:
        state_store.set_app_dir(tmp)
        state_store.set_registry_enabled(False)
        state_store.clear_state()
        license_core.set_revocation_dirs([os.path.join(tmp, "revocations")])
        keygen.set_ledger_path(os.path.join(tmp, "orders.jsonl"))

        # 显式检查 NTP 共识：旧写法 abs(trusted_now - local) < 1 天在 NTP
        # 全失败时恒为 0，是恒真假绿；也不该用可被回拨的作者机本地时钟
        # 去裁定 NTP 是否合理。
        # 第六轮 L2：本步骤必须**有可能失败**，且顺带验证签发时间 fail-closed。
        consensus = clock.ntp_consensus()
        clock.reset_cache()
        local_now = int(time.time())
        if consensus is None:
            step("可信时间源", True,
                 "NTP 不可达：可信时间链路未覆盖；注入本地时钟仅验证流程闭环"
                 "（真实首次激活/签发需联网）", skipped=True)
            clock.set_test_clock(lambda: int(time.time()))
        else:
            step("可信时间源", clock.cross_check_ok(consensus, local_now),
                 "NTP 多源共识=%d，本地时钟=%d，方向交叉校验%s"
                 % (consensus, local_now,
                    "通过" if clock.cross_check_ok(consensus, local_now) else "失败"))
            with mock.patch("licensing.clock._ntp_seconds", return_value=None):
                refused = False
                try:
                    clock.require_trusted_now()
                except RuntimeError:
                    refused = True
            step("签发时间 fail-closed", refused,
                 "NTP 全断时作者端必须拒绝签发")
            # 第 1 步与上面这步已用真实 NTP 覆盖可信时间链路；之后的
            # 签发/激活/续期/撤销改用确定性时钟，避免网络抖动让冒烟偶发失败。
            clock.set_test_clock(lambda: consensus)
            clock.reset_cache()

        request_text = fingerprint.make_request()
        req = fingerprint.parse_request(request_text)
        step("激活请求生成",
             len(req["fp"]) >= 2 and req["mid"].startswith("SZ-")
             and all(isinstance(x, str) and x for x in req["fp"]),
             "机器码=%s，组件=%d" % (req["mid"], len(req["fp"])))

        from licensing import public_key as pk_module
        pub = license_core.public_key()
        actual_fp = rsa_sign.public_fingerprint(pub)
        step("生产公钥已内置",
             int(pub["n"]) > 0 and actual_fp == str(pk_module.FINGERPRINT),
             "指纹=%s（与内置 FINGERPRINT %s）"
             % (actual_fp, "一致" if actual_fp == str(pk_module.FINGERPRINT)
                else "不一致"))

        token = keygen.issue("SMOKE001", request_text, "timed", 7 * 86400)
        lic_path = os.path.join(tmp, "SMOKE001.lic")
        with open(lic_path, "w", encoding="utf-8") as fh:
            fh.write(rsa_sign.split_lines(token) + "\n")
        with open(lic_path, "r", encoding="utf-8") as fh:
            lic_text = fh.read()
        ok, payload, why = rsa_sign.verify_token(lic_text, pub)
        step("签发与验签", ok and payload["seq"] == 1, why)

        ok, _, why = license_core.activate(token)
        step("客户端激活", ok, why)
        # 真实客户端启动走的是严格校时入口，冒烟必须覆盖同一条路径
        ok, why = license_core.check_license(strict_time=True)
        step("启动严格校时", ok, why)

        renewed = keygen.renew("SMOKE001", 30 * 86400)
        ok, payload, why = license_core.activate(renewed)
        step("续期 seq 递增", ok and payload["seq"] == 2, why)

        revoke, challenge = keygen.make_revocation("SMOKE001", "用户退款")
        ok, receipt, why = license_core.apply_revocation(revoke)
        step("导入撤销令", ok and bool(receipt["code"]), why)
        ok, why = license_core.check_license()
        step("撤销后停用", not ok, why)

        ok, why = keygen.verify_receipt("SMOKE001", receipt["code"],
                                        receipt["revoked_at"])
        step("退款回执复算", ok, why)

        rows = keygen.read_ledger()
        issue_rows = [r for r in rows if r.get("kind") == "issue"]
        revoke_rows = [r for r in rows if r.get("kind") == "revoke"]
        step("台账记录完整", len(issue_rows) == 2 and len(revoke_rows) == 1,
             "%d 签发 / %d 撤销" % (len(issue_rows), len(revoke_rows)))
        print("\n冒烟验证全部通过；生产数据未被触碰。")
        clock.set_test_clock(None)
        return 0


if __name__ == "__main__":
    sys.exit(main())
