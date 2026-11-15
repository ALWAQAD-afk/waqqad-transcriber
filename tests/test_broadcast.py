"""اختبارات إرسال الرسائل الجماعية: التحقق، التقدم، وإعادة المحاولة."""

import unittest

import broadcast


class FakeError(RuntimeError):
    pass


class TestCleanText(unittest.TestCase):
    def test_strips_surrounding_space(self):
        self.assertEqual(broadcast.clean_text("  مرحبًا  "), "مرحبًا")

    def test_rejects_empty_text(self):
        with self.assertRaises(ValueError):
            broadcast.clean_text("   ")
        with self.assertRaises(ValueError):
            broadcast.clean_text(None)

    def test_rejects_text_over_telegram_limit(self):
        with self.assertRaises(ValueError):
            broadcast.clean_text("ا" * (broadcast.MAX_TEXT_CHARS + 1))

    def test_accepts_text_at_limit(self):
        text = "ا" * broadcast.MAX_TEXT_CHARS
        self.assertEqual(broadcast.clean_text(text), text)

    def test_rejects_emoji_over_utf16_limit(self):
        # الرمز الواحد يساوي وحدتين في UTF-16، فيتجاوز الحد رغم قلة عدده.
        with self.assertRaises(ValueError):
            broadcast.clean_text("🙂" * (broadcast.MAX_TEXT_CHARS // 2 + 1))


class TestRetryAfter(unittest.TestCase):
    def test_extracts_retry_after(self):
        error = (
            'Telegram sendMessage HTTP 429: '
            '{"ok":false,"parameters":{"retry_after":7}}'
        )
        self.assertEqual(broadcast.retry_after_seconds(error), 7)

    def test_returns_none_without_retry_after(self):
        self.assertIsNone(broadcast.retry_after_seconds("HTTP 403 Forbidden"))
        self.assertIsNone(broadcast.retry_after_seconds(None))


class TestSendAll(unittest.TestCase):
    def test_sends_to_each_user_once(self):
        delivered = []

        result = broadcast.send_all(
            [1, 2, 2, 3],
            "نص",
            lambda uid, text: delivered.append((uid, text)),
            sleep=lambda seconds: None,
        )

        self.assertEqual([uid for uid, _ in delivered], ["1", "2", "3"])
        self.assertTrue(all(text == "نص" for _, text in delivered))
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["sent"], 3)
        self.assertEqual(result["failed"], 0)

    def test_counts_failures_without_stopping(self):
        def send(uid, text):
            if uid == "2":
                raise FakeError("403 blocked")

        result = broadcast.send_all(
            [1, 2, 3],
            "نص",
            send,
            sleep=lambda seconds: None,
        )

        self.assertEqual(result["sent"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["failed_ids"], ["2"])

    def test_retries_after_429_then_succeeds(self):
        attempts = []
        waits = []

        def send(uid, text):
            attempts.append(uid)
            if len(attempts) == 1:
                raise FakeError(
                    'HTTP 429: {"parameters":{"retry_after":2}}'
                )

        result = broadcast.send_all(
            [10],
            "نص",
            send,
            sleep=waits.append,
        )

        self.assertEqual(attempts, ["10", "10"])
        self.assertEqual(waits, [2])
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)

    def test_429_retry_waits_at_least_one_second(self):
        waits = []

        def send(uid, text):
            if not waits:
                raise FakeError('{"retry_after":0}')

        broadcast.send_all([10], "نص", send, sleep=waits.append)
        self.assertEqual(waits, [1])

    def test_gap_between_users_only(self):
        waits = []
        broadcast.send_all(
            [1, 2, 3],
            "نص",
            lambda uid, text: None,
            sleep=waits.append,
            gap=0.05,
        )
        self.assertEqual(waits, [0.05, 0.05])

    def test_progress_reports_every_user(self):
        progress = []
        broadcast.send_all(
            [1, 2],
            "نص",
            lambda uid, text: None,
            sleep=lambda seconds: None,
            on_progress=lambda *args: progress.append(args),
        )
        self.assertEqual(progress, [(1, 1, 0, 2), (2, 2, 0, 2)])


if __name__ == "__main__":
    unittest.main()
