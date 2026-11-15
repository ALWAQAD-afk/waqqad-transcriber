"""اختبارات أمر المراسلة الجماعية وزرّي التأكيد والإلغاء."""

import os
import time
import unittest
from unittest import mock

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456789:TESTBOTTOKENABCdefGhIjkLmNoPqRsTuVwXyZ",
    "WARRAQ_BOT_TOKEN": "456:TEST",
    "WARRAQ_STORAGE_CHAT_ID": "-1001",
    "WARRAQ_DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "TELEGRAM_WEBHOOK_SECRET": "secret",
    "WARRAQ_WEBHOOK_SECRET": "secret",
    "DEVELOPER_CHAT_ID": "7991076455",
})

import app


class BroadcastCommandTests(unittest.TestCase):
    def setUp(self):
        app._BROADCAST_DRAFT.clear()
        app._BROADCAST_PENDING.clear()
        app._BROADCAST_RUNNING = False

        self.tg = mock.patch.object(app, "telegram_send_message").start()
        self.edit = mock.patch.object(app, "telegram_edit_message").start()
        self.answer = mock.patch.object(app, "answer_callback").start()
        self.addCleanup(mock.patch.stopall)

    def test_non_developer_is_rejected(self):
        app.handle_broadcast_command(123, 456, "رسالة")

        self.tg.assert_called_once()
        self.assertIn("للمطوّر فقط", self.tg.call_args[0][1])

    def test_command_without_text_waits_for_next_message(self):
        app.handle_broadcast_command(7991076455, 7991076455, "")

        self.assertIn("7991076455", app._BROADCAST_DRAFT)
        self.assertIn("اكتب نص الرسالة", self.tg.call_args[0][1])

    def test_preview_stores_token_and_recipient_count(self):
        with mock.patch.object(
            app, "_broadcast_recipients", return_value=["11", "22"]
        ):
            app._broadcast_preview(7991076455, 7991076455, "  مرحبًا  ")

        self.assertEqual(len(app._BROADCAST_PENDING), 1)
        token, entry = next(iter(app._BROADCAST_PENDING.items()))
        self.assertEqual(entry["text"], "مرحبًا")
        self.assertEqual(entry["recipients"], ["11", "22"])

        markup = str(self.tg.call_args.kwargs["reply_markup"])
        self.assertIn(f"bc:yes:{token}", markup)
        self.assertIn(f"bc:no:{token}", markup)
        self.assertIn("👥 المستلمون: 2", self.tg.call_args[0][1])

    def test_empty_recipients_does_not_create_preview(self):
        with mock.patch.object(app, "_broadcast_recipients", return_value=[]):
            app._broadcast_preview(7991076455, 7991076455, "مرحبًا")

        self.assertEqual(app._BROADCAST_PENDING, {})
        self.assertIn("لا يوجد مستخدمون", self.tg.call_args[0][1])

    def test_cancel_marks_message_cancelled(self):
        app._BROADCAST_PENDING["tok"] = {
            "text": "نص",
            "recipients": ["1"],
            "created_at": time.time(),
        }

        app._handle_broadcast_callback(
            "cb", "bc:no:tok", 7991076455, 7991076455, 5
        )

        self.assertEqual(app._BROADCAST_PENDING, {})
        self.answer.assert_called_once()
        self.assertIn("أُلغيت", self.edit.call_args[0][2])

    def test_expired_token_does_not_send(self):
        app._BROADCAST_PENDING["old"] = {
            "text": "نص",
            "recipients": ["1"],
            "created_at": time.time() - app.BROADCAST_CONFIRM_SECONDS - 1,
        }

        app._handle_broadcast_callback(
            "cb", "bc:yes:old", 7991076455, 7991076455, 5
        )

        self.assertIn("انتهت", self.answer.call_args[0][1])

    def test_confirm_starts_background_thread(self):
        app._BROADCAST_PENDING["tok"] = {
            "text": "نص",
            "recipients": ["1", "2"],
            "created_at": time.time(),
        }

        with mock.patch.object(app.threading, "Thread") as thread_cls:
            app._handle_broadcast_callback(
                "cb", "bc:yes:tok", 7991076455, 7991076455, 5
            )

        thread_cls.assert_called_once()
        self.assertTrue(app._BROADCAST_RUNNING)
        self.tg.assert_not_called()

    def test_finished_broadcast_reports_counts(self):
        app._BROADCAST_RUNNING = True
        result = {
            "total": 3,
            "sent": 2,
            "failed": 1,
            "failed_ids": ["3"],
        }
        entry = {"text": "نص", "recipients": ["1", "2", "3"]}

        with mock.patch.object(
            app.broadcast_util, "send_all", return_value=result
        ):
            app._run_broadcast(7991076455, 9, entry)

        final_text = self.edit.call_args[0][2]
        self.assertIn("أُرسلت: 2", final_text)
        self.assertIn("تعذّر: 1", final_text)
        self.assertFalse(app._BROADCAST_RUNNING)


if __name__ == "__main__":
    unittest.main()
