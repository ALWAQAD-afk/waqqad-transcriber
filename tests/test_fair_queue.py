import threading
import time
import unittest
from unittest import mock

import job_queue


class TestFairJobQueue(unittest.TestCase):
    def setUp(self):
        job_queue._reset_state_for_tests()

    def tearDown(self):
        job_queue._reset_state_for_tests()

    def test_fair_order_simulation_different_users(self):
        # Prevent worker from picking up jobs by holding a lock in a controlled worker,
        # or testing order without starting workers.
        # Enqueue multiple jobs for user A, then one for user B
        now = time.time()
        job_queue._WAITING_TASKS.append({
            "job_id": "A1",
            "user_id": "user_a",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.1,
        })
        job_queue._WAITING_TASKS.append({
            "job_id": "A2",
            "user_id": "user_a",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.2,
        })
        job_queue._WAITING_TASKS.append({
            "job_id": "A3",
            "user_id": "user_a",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.3,
        })
        job_queue._WAITING_TASKS.append({
            "job_id": "B1",
            "user_id": "user_b",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.4,
        })

        order = job_queue._get_fair_order_of_waiting()
        # A1 has 0 running, B1 has 0 running. A1 was enqueued first, so A1 is first.
        # Then user_a has count 1, while user_b has count 0. So B1 is chosen next!
        # Then user_a has count 1, so A2, then A3.
        self.assertEqual(order, ["A1", "B1", "A2", "A3"])

    def test_fifo_preserved_per_user(self):
        now = time.time()
        for i in range(1, 5):
            job_queue._WAITING_TASKS.append({
                "job_id": f"A{i}",
                "user_id": "user_a",
                "fn": lambda: None,
                "args": (),
                "kwargs": {},
                "enqueued_at": now + i,
            })
        order = job_queue._get_fair_order_of_waiting()
        self.assertEqual(order, ["A1", "A2", "A3", "A4"])

    def test_position_reflects_fair_ordering(self):
        now = time.time()
        job_queue._WAITING_TASKS.append({
            "job_id": "A1",
            "user_id": "user_a",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.1,
        })
        job_queue._WAITING_TASKS.append({
            "job_id": "A2",
            "user_id": "user_a",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.2,
        })
        job_queue._WAITING_TASKS.append({
            "job_id": "B1",
            "user_id": "user_b",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.3,
        })

        self.assertEqual(job_queue.position("A1"), 1)
        self.assertEqual(job_queue.position("B1"), 2)
        self.assertEqual(job_queue.position("A2"), 3)

    def test_running_job_counts_towards_user_penalty(self):
        now = time.time()
        # user_a is already running a job
        job_queue._RUNNING["A_running"] = {
            "started_at": now,
            "user_id": "user_a",
        }
        # user_a and user_b enqueue
        job_queue._WAITING_TASKS.append({
            "job_id": "A1",
            "user_id": "user_a",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.1,
        })
        job_queue._WAITING_TASKS.append({
            "job_id": "B1",
            "user_id": "user_b",
            "fn": lambda: None,
            "args": (),
            "kwargs": {},
            "enqueued_at": now + 0.2,
        })

        # user_b has 0 running jobs, while user_a has 1 running job.
        # B1 should jump ahead of A1 even though A1 was enqueued first!
        order = job_queue._get_fair_order_of_waiting()
        self.assertEqual(order, ["B1", "A1"])


class TestWorkerFairExecution(unittest.TestCase):
    def setUp(self):
        job_queue._reset_state_for_tests()

    def tearDown(self):
        job_queue._reset_state_for_tests()

    def test_fair_execution_order_with_workers(self):
        execution_order = []
        a1_started = threading.Event()
        can_a1_finish = threading.Event()
        all_done = threading.Event()

        def task_a1():
            execution_order.append("A1")
            a1_started.set()
            can_a1_finish.wait(timeout=2)

        def task_a2():
            execution_order.append("A2")
            all_done.set()

        def task_b1():
            execution_order.append("B1")

        # 1 worker to strictly serialize execution
        with mock.patch.object(job_queue, "WORKERS", 1):
            job_queue.enqueue("A1", task_a1, user_id="user_a")
            self.assertTrue(a1_started.wait(timeout=2), "A1 should start immediately")

            # While A1 is running (user_a running count = 1), user A enqueues A2, and user B enqueues B1
            job_queue.enqueue("A2", task_a2, user_id="user_a")
            job_queue.enqueue("B1", task_b1, user_id="user_b")

            # Now release A1
            can_a1_finish.set()

            # Wait for all tasks to complete
            self.assertTrue(all_done.wait(timeout=3), "Tasks did not complete in time")

        # B1 must execute BEFORE A2 because user_b had 0 running tasks, whereas user_a was executing A1!
        self.assertEqual(execution_order, ["A1", "B1", "A2"])


if __name__ == "__main__":
    unittest.main()

