import os
import time
import unittest
from unittest import mock
import json

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:TEST",
    "WARRAQ_BOT_TOKEN": "456:TEST",
    "WARRAQ_STORAGE_CHAT_ID": "-1001",
    "WARRAQ_DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "TELEGRAM_WEBHOOK_SECRET": "secret",
    "WARRAQ_WEBHOOK_SECRET": "secret",
})

import app


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConn:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class DeepgramReaperTests(unittest.TestCase):
    def test_reap_stale_jobs_marks_failed(self):
        stale_data = {
            "status": "processing",
            "stage": "waiting_deepgram",
            "deepgram_submitted_at": time.time() - 7200,  # 2 hours ago
            "telegram_user_id": 99999,
            "original_filename": "lecture.mp3",
        }
        fake_conn = FakeConn([{"job_id": "stale-1", "data": json.dumps(stale_data)}])

        with mock.patch.object(app, "get_db_connection", return_value=fake_conn), \
             mock.patch.object(app, "cleanup_job_workdir") as mock_cleanup, \
             mock.patch.object(app, "telegram_send_message") as mock_tg:
            reaped = app.reap_stale_deepgram_jobs(max_age_seconds=3600)

        self.assertEqual(reaped, ["stale-1"])
        mock_cleanup.assert_called_once_with("stale-1")
        mock_tg.assert_called_once()
        self.assertTrue(fake_conn.committed)

    def test_fresh_jobs_are_not_reaped(self):
        fresh_data = {
            "status": "processing",
            "stage": "waiting_deepgram",
            "deepgram_submitted_at": time.time() - 300,  # 5 mins ago
        }
        fake_conn = FakeConn([{"job_id": "fresh-1", "data": json.dumps(fresh_data)}])

        with mock.patch.object(app, "get_db_connection", return_value=fake_conn), \
             mock.patch.object(app, "cleanup_job_workdir") as mock_cleanup:
            reaped = app.reap_stale_deepgram_jobs(max_age_seconds=3600)

        self.assertEqual(reaped, [])
        mock_cleanup.assert_not_called()

    def test_late_callback_on_failed_job_is_ignored(self):
        with app.app.test_request_context(
            "/deepgram-callback/job-timed-out?token=valid-token",
            method="POST",
            json={"results": {"channels": [{"alternatives": [{"transcript": "late text"}]}]}}
        ):
            with mock.patch.object(app, "_verify_callback_token", return_value=True), \
                 mock.patch.object(app, "get_job", return_value={"status": "failed", "stage": "error"}), \
                 mock.patch.object(app, "finalize_transcript") as mock_finalize:
                response, status = app.deepgram_callback("job-timed-out")

                self.assertEqual(status, 200)
                data = response.get_json()
                self.assertEqual(data.get("status"), "ignored")
                mock_finalize.assert_not_called()

    def test_duplicate_callback_on_completed_job_is_ignored(self):
        with app.app.test_request_context(
            "/deepgram-callback/job-done?token=valid-token",
            method="POST",
            json={"results": {"channels": [{"alternatives": [{"transcript": "duplicate text"}]}]}}
        ):
            with mock.patch.object(app, "_verify_callback_token", return_value=True), \
                 mock.patch.object(app, "get_job", return_value={"status": "completed", "stage": "done"}), \
                 mock.patch.object(app, "finalize_transcript") as mock_finalize:
                response, status = app.deepgram_callback("job-done")

                self.assertEqual(status, 200)
                data = response.get_json()
                self.assertEqual(data.get("status"), "ignored")
                mock_finalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
