import os
import unittest
from unittest import mock
import concurrent.futures

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:TEST",
    "WARRAQ_BOT_TOKEN": "456:TEST",
    "WARRAQ_STORAGE_CHAT_ID": "-1001",
    "WARRAQ_DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "TELEGRAM_WEBHOOK_SECRET": "secret",
    "WARRAQ_WEBHOOK_SECRET": "secret",
})

import db


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self, conninfo, **kwargs):
        self.conninfo = conninfo
        self.closed = False
        self.borrowed = 0
        self.returned = 0

    class _Context:
        def __init__(self, outer):
            self.outer = outer

        def __enter__(self):
            self.outer.borrowed += 1
            return FakeConnection()

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.outer.returned += 1

    def connection(self):
        return self._Context(self)

    def close(self):
        self.closed = True


class DBPoolTests(unittest.TestCase):
    def setUp(self):
        db.close_all_pools()

    def tearDown(self):
        db.close_all_pools()

    def test_get_pool_singleton_per_url(self):
        with mock.patch("db.ConnectionPool", side_effect=FakePool):
            pool1 = db.get_pool("postgresql://fake/db1")
            pool2 = db.get_pool("postgresql://fake/db1")
            pool3 = db.get_pool("postgresql://fake/db2")

            self.assertIs(pool1, pool2)
            self.assertIsNot(pool1, pool3)

    def test_get_db_connection_context_manager(self):
        with mock.patch("db.ConnectionPool", side_effect=FakePool):
            pool = db.get_pool("postgresql://fake/db")
            with db.get_db_connection("postgresql://fake/db") as conn:
                self.assertIsInstance(conn, FakeConnection)
                self.assertEqual(pool.borrowed, 1)
                self.assertEqual(pool.returned, 0)

            self.assertEqual(pool.returned, 1)

    def test_concurrent_connection_usage(self):
        with mock.patch("db.ConnectionPool", side_effect=FakePool):
            pool = db.get_pool("postgresql://fake/db")

            def worker(index):
                with db.get_db_connection("postgresql://fake/db") as conn:
                    return index

            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(worker, range(10)))

            self.assertEqual(results, list(range(10)))
            self.assertEqual(pool.borrowed, 10)
            self.assertEqual(pool.returned, 10)

    def test_close_all_pools(self):
        with mock.patch("db.ConnectionPool", side_effect=FakePool):
            pool1 = db.get_pool("postgresql://fake/db1")
            pool2 = db.get_pool("postgresql://fake/db2")

            db.close_all_pools()
            self.assertTrue(pool1.closed)
            self.assertTrue(pool2.closed)
            self.assertEqual(len(db._POOLS), 0)


if __name__ == "__main__":
    unittest.main()
