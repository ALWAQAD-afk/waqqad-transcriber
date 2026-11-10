import os
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:TEST",
    "WARRAQ_BOT_TOKEN": "456:TEST",
    "WARRAQ_STORAGE_CHAT_ID": "-1001",
    "WARRAQ_DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "DATABASE_URL": "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "TELEGRAM_WEBHOOK_SECRET": "secret",
    "WARRAQ_WEBHOOK_SECRET": "secret",
    "COHERE_API_KEYS": "key1,key2,key3",
    "COHERE_CONCURRENCY": "3",
})

import app


class CohereParallelTests(unittest.TestCase):
    def test_parts_preserve_order_even_with_out_of_order_completion(self):
        parts = [
            Path("/tmp/part_001.mp3"),
            Path("/tmp/part_002.mp3"),
            Path("/tmp/part_003.mp3"),
        ]

        # Part 1 takes 0.05s, Part 2 takes 0.01s (finishes first!), Part 3 takes 0.03s
        def fake_transcribe(job_id, part, part_index, part_count):
            if part_index == 1:
                time.sleep(0.05)
                return "النص الأول", 1
            elif part_index == 2:
                time.sleep(0.01)
                return "النص الثاني", 2
            else:
                time.sleep(0.03)
                return "النص الثالث", 3

        finalized = {}

        def fake_finalize(job_id, text, engine):
            finalized["text"] = text
            finalized["engine"] = engine

        with mock.patch.object(app, "prepare_cohere_parts", return_value=parts), \
             mock.patch.object(app, "cohere_transcribe_part", side_effect=fake_transcribe), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "finalize_transcript", side_effect=fake_finalize):
            app.process_transcription_job("test-parallel-job", Path("/tmp/input.mp3"))

        expected = "النص الأول\n\nالنص الثاني\n\nالنص الثالث"
        self.assertEqual(finalized.get("text"), expected)
        self.assertEqual(finalized.get("engine"), "cohere")

    def test_cohere_unavailable_propagates_to_deepgram_fallback(self):
        parts = [Path("/tmp/part_001.mp3"), Path("/tmp/part_002.mp3")]

        def failing_transcribe(job_id, part, part_index, part_count):
            raise app.CohereUnavailableError("Cohere quota exhausted")

        with mock.patch.object(app, "prepare_cohere_parts", return_value=parts), \
             mock.patch.object(app, "cohere_transcribe_part", side_effect=failing_transcribe), \
             mock.patch.object(app, "update_job"), \
             mock.patch.object(app, "submit_to_deepgram") as mock_deepgram:
            app.process_transcription_job("test-fallback-job", Path("/tmp/input.mp3"))

        mock_deepgram.assert_called_once()
        self.assertIn("fallback_reason", mock_deepgram.call_args[1])

    def test_local_error_does_not_trigger_deepgram(self):
        parts = [Path("/tmp/part_001.mp3")]

        def failing_transcribe(job_id, part, part_index, part_count):
            raise app.LocalProcessingError("Malformed audio data")

        with mock.patch.object(app, "prepare_cohere_parts", return_value=parts), \
             mock.patch.object(app, "cohere_transcribe_part", side_effect=failing_transcribe), \
             mock.patch.object(app, "update_job") as mock_update, \
             mock.patch.object(app, "submit_to_deepgram") as mock_deepgram, \
             mock.patch.object(app, "log_transcription_failure"):
            app.process_transcription_job("test-local-err", Path("/tmp/input.mp3"))

        mock_deepgram.assert_not_called()


if __name__ == "__main__":
    unittest.main()
