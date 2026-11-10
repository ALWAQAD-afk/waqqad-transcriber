import unittest
from unittest import mock
from pathlib import Path
import shutil

import app


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.job_id = "test-cleanup-job"
        self.work_dir = app.get_work_dir(self.job_id)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "sample.txt").write_text("hello", encoding="utf-8")

    def tearDown(self):
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir, ignore_errors=True)

    def test_cleanup_removes_existing_workdir(self):
        self.assertTrue(self.work_dir.exists())
        app.cleanup_job_workdir(self.job_id)
        self.assertFalse(self.work_dir.exists())

    def test_cleanup_is_idempotent_on_missing_dir(self):
        app.cleanup_job_workdir(self.job_id)
        self.assertFalse(self.work_dir.exists())
        # Should not raise
        app.cleanup_job_workdir(self.job_id)
        app.cleanup_job_workdir("")
        app.cleanup_job_workdir(None)

    def test_finalize_transcript_cleans_up_when_refine_disabled(self):
        output_file = self.work_dir / "transcript.txt"
        output_file.write_text("test", encoding="utf-8")

        with mock.patch.object(app, "REFINE_ENABLED", False), \
             mock.patch.object(app, "get_job", return_value={"original_filename": "audio.mp3"}), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app.transcript_store, "save_transcript"), \
             mock.patch.object(app, "_archive_transcript"), \
             mock.patch.object(app, "send_transcript_to_telegram"):
            app.finalize_transcript(self.job_id, "test transcript", "cohere")

        self.assertFalse(self.work_dir.exists())

    def test_send_refined_versions_async_cleans_up_in_finally(self):
        output_file = self.work_dir / "transcript.txt"
        output_file.write_text("نص الاختبار للتنقيح", encoding="utf-8")

        with mock.patch.object(app, "REFINE_ENABLED", True), \
             mock.patch.object(app, "refine_document", return_value={"conservative": "نص", "liberal": "نص"}), \
             mock.patch.object(app, "get_job", return_value={"original_filename": "audio.mp3", "telegram_user_id": 12345}), \
             mock.patch.object(app, "send_status_message"), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "telegram_send_message"), \
             mock.patch.object(app, "delete_status_message"), \
             mock.patch.object(app, "send_feedback_prompt"):
            app.send_refined_versions_async(self.job_id, str(output_file))

        self.assertFalse(self.work_dir.exists())


if __name__ == "__main__":
    unittest.main()
