"""odoo_inspect.py 的單元測試（不需要叢集、不需要 psycopg2）。"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import odoo_inspect  # noqa: E402


class FakeOperationalError(Exception):
    pass


def fake_psycopg2(failures):
    """前 failures 次 connect 丟 OperationalError，之後回傳一個假連線。"""
    calls = []

    def connect(**kw):
        calls.append(kw)
        if len(calls) <= failures:
            raise FakeOperationalError("connection to server failed: Connection refused\n\tIs the server running?")
        return "conn"

    return types.SimpleNamespace(connect=connect, OperationalError=FakeOperationalError), calls


class ConnectRetryTest(unittest.TestCase):
    def test_retries_until_reachable(self):
        mod, calls = fake_psycopg2(failures=2)
        with mock.patch.dict(sys.modules, {"psycopg2": mod}), mock.patch.object(odoo_inspect.time, "sleep") as sleep:
            conn = odoo_inspect.connect("odoo", {"host": "pg", "user": "odoo"}, attempts=5, delay=2.0)
        self.assertEqual(conn, "conn")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], {"dbname": "odoo", "host": "pg", "user": "odoo"})
        self.assertEqual(sleep.call_count, 2)

    def test_gives_up_after_attempts(self):
        mod, calls = fake_psycopg2(failures=99)
        with mock.patch.dict(sys.modules, {"psycopg2": mod}), mock.patch.object(odoo_inspect.time, "sleep"):
            with self.assertRaises(FakeOperationalError):
                odoo_inspect.connect("odoo", {}, attempts=3, delay=0)
        self.assertEqual(len(calls), 3)


class FakeHTTPConnection:
    """前 refusals 次 request 丟 ConnectionRefusedError，之後 /healthz 回 200。"""
    attempts = 0
    refusals = 0

    def __init__(self, host, port, timeout):
        self.host, self.port = host, port

    def request(self, method, path):
        type(self).attempts += 1
        if type(self).attempts <= type(self).refusals:
            raise ConnectionRefusedError(111, "Connection refused")
        self.path = path

    def getresponse(self):
        return types.SimpleNamespace(status=200 if self.path == "/healthz" else 404)

    def close(self):
        pass


class WaitForReceiverTest(unittest.TestCase):
    URL = "http://standalone-odoo-receiver.ns.svc.cluster.local:8080/p1"

    def run_with(self, refusals, attempts):
        FakeHTTPConnection.attempts, FakeHTTPConnection.refusals = 0, refusals
        with mock.patch.object(odoo_inspect.http.client, "HTTPConnection", FakeHTTPConnection), \
                mock.patch.object(odoo_inspect.time, "sleep"):
            odoo_inspect.wait_for_receiver(self.URL, attempts=attempts, delay=0)

    def test_waits_until_healthz_answers(self):
        self.run_with(refusals=3, attempts=10)
        self.assertEqual(FakeHTTPConnection.attempts, 4)

    def test_gives_up_with_clear_error(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_with(refusals=99, attempts=3)
        self.assertIn("unreachable", str(cm.exception))
        self.assertEqual(FakeHTTPConnection.attempts, 3)


if __name__ == "__main__":
    unittest.main()


class ClassifyPosSessionsTest(unittest.TestCase):
    """#1758：opening_control、沒開始過、0 訂單的 session 不擋凍結；其餘未關帳的都算開著。"""

    def test_idle_opening_control_is_not_open(self):
        rows = [(185, "餐廳/00005", "opening_control", False, 0)]
        self.assertEqual(odoo_inspect.classify_pos_sessions(rows), (0, [{"id": 185, "name": "餐廳/00005"}]))

    def test_everything_else_counts_as_open(self):
        rows = [
            (1, "A", "opened", True, 3),
            (2, "B", "closing_control", True, 0),
            (3, "C", "opening_control", True, 0),    # 已有 start_at：開過帳
            (4, "D", "opening_control", False, 2),   # 有訂單：保守當作開著
            (5, "E", "opening_control", False, 0),
        ]
        self.assertEqual(odoo_inspect.classify_pos_sessions(rows), (4, [{"id": 5, "name": "E"}]))

    def test_empty(self):
        self.assertEqual(odoo_inspect.classify_pos_sessions([]), (0, []))
