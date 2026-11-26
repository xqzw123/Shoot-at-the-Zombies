"""运行全部授权测试：python tests/run_tests.py"""
from __future__ import annotations

import os
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    suite = unittest.TestLoader().discover(os.path.join(ROOT, "tests"),
                                          pattern="test_*.py", top_level_dir=ROOT)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    # 夹具必须停掉所有授权线程：残留线程会以真实 %APPDATA% / HKCU 为目标
    # 写状态，还会让后续用例的 start_heartbeat() 因 is_alive() 短路而假绿。
    leftovers = [t.name for t in threading.enumerate()
                 if t is not threading.current_thread() and t.is_alive() and
                 (t.name.startswith(("license-", "ThreadPoolExecutor"))
                  or not t.daemon)]
    if leftovers:
        print("测试结束仍有授权线程残留：%s" % ", ".join(leftovers))
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
