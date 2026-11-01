import threading
import unittest
from unittest import mock

import job_queue


class TestQueueAccounting(unittest.TestCase):
    """اختبارات الحساب دون تشغيل عمال حقيقيين."""

    def setUp(self):
        job_queue._reset_state_for_tests()
        patcher = mock.patch.object(job_queue, "start", lambda: None)
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        job_queue._reset_state_for_tests()

    def test_positions_follow_arrival_order(self):
        first = job_queue.enqueue("job-a", lambda: None)
        second = job_queue.enqueue("job-b", lambda: None)
        third = job_queue.enqueue("job-c", lambda: None)

        self.assertEqual([first, second, third], [1, 2, 3])
        self.assertEqual(job_queue.position("job-a"), 1)
        self.assertEqual(job_queue.position("job-c"), 3)
        self.assertEqual(job_queue.depth(), 3)

    def test_position_is_none_for_unknown_job(self):
        job_queue.enqueue("job-a", lambda: None)
        self.assertIsNone(job_queue.position("missing"))

    def test_estimate_wait_needs_history(self):
        self.assertIsNone(job_queue.estimate_wait(2))

    def test_estimate_wait_uses_average_and_workers(self):
        with mock.patch.object(job_queue, "WORKERS", 2), mock.patch.object(
            job_queue, "average_seconds", lambda: 120.0
        ):
            self.assertEqual(job_queue.estimate_wait(1), 120)
            self.assertEqual(job_queue.estimate_wait(2), 120)
            self.assertEqual(job_queue.estimate_wait(3), 240)


class TestWorkerIsolation(unittest.TestCase):
    """عامل واحد: مهمة تفشل يجب ألا تمنع التي بعدها."""

    def setUp(self):
        job_queue._reset_state_for_tests()

    def test_failure_does_not_stop_following_job(self):
        done = threading.Event()
        seen = []

        def failing():
            seen.append("failed")
            raise RuntimeError("boom")

        def succeeding():
            seen.append("ok")
            done.set()

        with mock.patch.object(job_queue, "WORKERS", 1):
            job_queue.enqueue("bad", failing)
            job_queue.enqueue("good", succeeding)
            self.assertTrue(done.wait(timeout=5), "second job never ran")

        self.assertEqual(seen, ["failed", "ok"])
        job_queue._reset_state_for_tests()


if __name__ == "__main__":
    unittest.main()
