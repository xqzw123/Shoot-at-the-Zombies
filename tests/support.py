"""TDD 公共夹具：把时间、硬件指纹、状态目录、注册表、台账全部隔离。

生产代码只通过模块级 setter 注入，测试不触碰真实 APPDATA / 注册表 / 网络。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest

from licensing import rsa_sign


def isolated_release_paths(tmp: str) -> dict:
    """在临时目录造一套自洽的「状态密钥三件套 + 空产物目录」。

    发布前检查类用例不得依赖交付机上的 private.json / state_secret.py /
    dist 产物 / PyInstaller；干净 clone 也要能跑绿（第五轮工程 M1）。
    """
    keys = os.path.join(tmp, "keys")
    os.makedirs(keys, exist_ok=True)
    secret = "a" * 64
    module = os.path.join(tmp, "state_secret.py")
    with open(module, "w", encoding="utf-8") as fh:
        fh.write('STATE_SECRET = "%s"\n' % secret)
    backup = os.path.join(keys, "state_secret.txt")
    with open(backup, "w", encoding="utf-8") as fh:
        fh.write(secret)
    anchor = os.path.join(tmp, "state_secret.fingerprint")
    with open(anchor, "w", encoding="utf-8") as fh:
        fh.write(hashlib.sha256(bytes.fromhex(secret)).hexdigest()[:32])
    out = os.path.join(tmp, "dist")
    os.makedirs(out, exist_ok=True)
    return {"STATE_SECRET_MODULE": module,
            "STATE_SECRET_BACKUP": backup,
            "STATE_SECRET_FINGERPRINT": anchor,
            "OUT_DIR": out}

_KEYPAIR = rsa_sign.generate_keypair()
PUBLIC = {"n": _KEYPAIR["n"], "e": _KEYPAIR["e"]}

BASE_TIME = 1764000000
FIXED_FP = [
    "1111111111111111",
    "2222222222222222",
    "3333333333333333",
    "4444444444444444",
    "5555555555555555",
]
OTHER_FP = [
    "aaaaaaaaaaaaaaaa",
    "bbbbbbbbbbbbbbbb",
    "cccccccccccccccc",
    "dddddddddddddddd",
    "eeeeeeeeeeeeeeee",
]


def keypair():
    """全测试套件共用 import 时生成的一次 2048 位密钥，避免重复素性检测。"""
    return _KEYPAIR


class LicensingTestCase(unittest.TestCase):
    def setUp(self):
        from licensing import clock, fingerprint, state_store, license_core, keygen

        self.kp = keypair()
        self.pub = PUBLIC
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [BASE_TIME]

        clock.set_test_clock(lambda: self.now[0])
        clock.set_ntp_enabled(False)
        fingerprint.set_test_components(list(FIXED_FP))
        state_store.set_app_dir(self.tmp.name)
        state_store.set_registry_enabled(False)
        state_store.clear_state()
        license_core.set_public_key(self.pub)
        license_core.set_revocation_dirs([os.path.join(self.tmp.name, "revocations")])
        keygen.set_keys(self.kp, self.pub)
        keygen.set_keys_dir(os.path.join(self.tmp.name, "keys"))
        keygen.set_ledger_path(os.path.join(self.tmp.name, "orders.jsonl"))
        self.addCleanup(self._reset_hooks)

    def _reset_hooks(self):
        from licensing import clock, fingerprint, state_store, license_core, keygen
        # 绝不在这里调用 clear_state()：此时 override 已清空、注册表已恢复，
        # 会误删真实 %APPDATA%/state.json 与真实 HKCU 键。
        # 心跳/时间刷新线程必须在恢复真实路径之前停掉，否则残留线程会以
        # 真实 %APPDATA% / HKCU 为目标写状态，还会让后续 start_* 假绿。
        license_core.stop_heartbeat()
        clock.set_test_clock(None)
        # 先停刷新线程再恢复 NTP 开关：否则旧线程可能在 cleanup 窗口发起
        # 真实网络 I/O（最多 ~2.5 秒），拖慢测试并产生不可预期的缓存写入。
        clock.stop_time_refresher()
        clock.set_ntp_enabled(True)
        clock.set_time_anchor(None)
        clock.reset_cache()
        fingerprint.set_test_components(None)
        state_store.set_app_dir(None)
        license_core.set_public_key(None)
        license_core.set_revocation_dirs(None)
        keygen.set_keys(None, None)
        keygen.set_keys_dir(None)
        keygen.set_ledger_path(None)
        state_store.set_registry_enabled(True)

    def advance(self, seconds):
        self.now[0] += seconds
        return self.now[0]

    def request(self, fp=None):
        from licensing import fingerprint
        return fingerprint.make_request(fp)

    def issue(self, order="A001", typ="timed", days=7, **kwargs):
        from licensing import keygen
        return keygen.issue(order=order, request_text=self.request(), type=typ,
                           duration_seconds=days * 86400, **kwargs)

    def activate(self, token):
        from licensing import license_core
        return license_core.activate(token)

    def base_state(self, **overrides):
        state = {
            "v": 1,
            "order": "A001",
            "max_seq": 1,
            "activated_at": BASE_TIME,
            "last_seen": BASE_TIME,
            "used_seconds": 0,
            "revoked": False,
            "revoked_at": 0,
            "challenge": "",
            "token": "",
        }
        state.update(overrides)
        return state
