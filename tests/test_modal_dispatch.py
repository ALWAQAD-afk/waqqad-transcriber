import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

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


class ModalDispatchTests(unittest.TestCase):
    def setUp(self):
        self.job_id = "modal-dispatch-test"
        self.fake_modal = types.ModuleType("modal_app")
        self.fake_modal.process_job = SimpleNamespace(
            spawn=mock.Mock(return_value=SimpleNamespace(object_id="fc-test"))
        )
        self.modules_patcher = mock.patch.dict(
            sys.modules, {"modal_app": self.fake_modal}
        )
        self.modules_patcher.start()
        self.addCleanup(self.modules_patcher.stop)

    def test_modal_success_never_calls_render_fallback(self):
        with mock.patch.object(app, "MODAL_DISPATCH_ENABLED", True), \
             mock.patch.object(app, "_render_fallback_ready", return_value=True), \
             mock.patch.object(app, "_post_render_fallback") as fallback, \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "cleanup_job_workdir"):
            result = app.dispatch_heavy_job(
                "url", self.job_id, "42", url="https://example.com/a.mp3"
            )

        self.assertEqual(result, "modal")
        self.fake_modal.process_job.spawn.assert_called_once()
        fallback.assert_not_called()

    def test_bookkeeping_failure_after_spawn_does_not_fallback(self):
        with mock.patch.object(app, "MODAL_DISPATCH_ENABLED", True), \
             mock.patch.object(app, "_render_fallback_ready", return_value=True), \
             mock.patch.object(app, "_post_render_fallback") as fallback, \
             mock.patch.object(app, "update_job", side_effect=RuntimeError("db")), \
             mock.patch.object(app, "cleanup_job_workdir"):
            result = app.dispatch_heavy_job(
                "url", self.job_id, "42", url="https://example.com/a.mp3"
            )

        self.assertEqual(result, "modal")
        self.fake_modal.process_job.spawn.assert_called_once()
        fallback.assert_not_called()

    def test_modal_failure_uses_render_fallback(self):
        self.fake_modal.process_job.spawn.side_effect = ConnectionError("modal down")

        with mock.patch.object(app, "MODAL_DISPATCH_ENABLED", True), \
             mock.patch.object(app, "_render_fallback_ready", return_value=True), \
             mock.patch.object(app, "_post_render_fallback") as fallback, \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "cleanup_job_workdir"):
            result = app.dispatch_heavy_job(
                "url", self.job_id, "42", url="https://example.com/a.mp3"
            )

        self.assertEqual(result, "render_fallback")
        fallback.assert_called_once()

    def test_modal_and_render_failure_returns_clear_failure(self):
        self.fake_modal.process_job.spawn.side_effect = TimeoutError("modal timeout")

        with mock.patch.object(app, "MODAL_DISPATCH_ENABLED", True), \
             mock.patch.object(app, "_render_fallback_ready", return_value=False), \
             mock.patch.object(app, "update_job") as update_job, \
             mock.patch.object(app, "log_transcription_failure"), \
             mock.patch.object(app, "update_status_message"), \
             mock.patch.object(app, "cleanup_job_workdir") as cleanup:
            result = app.dispatch_heavy_job(
                "url", self.job_id, "42", url="https://example.com/a.mp3"
            )

        self.assertEqual(result, "failed")
        cleanup.assert_called()
        failed_calls = [
            call for call in update_job.call_args_list
            if call.kwargs.get("status") == "failed"
        ]
        self.assertTrue(failed_calls)
        self.assertIn(
            "تعذّر تشغيل المهمة على Modal",
            failed_calls[-1].kwargs["error"],
        )

    def test_upload_payload_carries_bytes_to_modal(self):
        with mock.patch.object(app, "MODAL_DISPATCH_ENABLED", True), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "cleanup_job_workdir"), \
             mock.patch.object(app, "get_work_dir") as get_work_dir:
            path = app.TMP_DIR / "modal-dispatch-upload.mp3"
            path.write_bytes(b"0123456789")
            self.addCleanup(lambda: path.unlink(missing_ok=True))
            get_work_dir.return_value = app.TMP_DIR

            result = app.dispatch_heavy_job(
                "upload", self.job_id, "42", file_path=path
            )

        self.assertEqual(result, "modal")
        payload = self.fake_modal.process_job.spawn.call_args.args[0]
        self.assertEqual(payload["file_bytes"], b"0123456789")
        self.assertEqual(payload["kind"], "upload")


if __name__ == "__main__":
    unittest.main()
