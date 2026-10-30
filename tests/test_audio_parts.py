import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:TEST",
    "WARRAQ_BOT_TOKEN": "456:TEST",
    "WARRAQ_STORAGE_CHAT_ID": "-1001",
    "WARRAQ_DATABASE_URL":
        "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "DATABASE_URL":
        "postgresql://u:p@127.0.0.1:1/db?connect_timeout=1",
    "TELEGRAM_WEBHOOK_SECRET": "secret",
    "WARRAQ_WEBHOOK_SECRET": "secret",
})

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


class AudioPartsTests(unittest.TestCase):
    def setUp(self):
        self.job_ids = []
        self.updates = {}
        patcher = mock.patch.object(
            app, "update_job",
            side_effect=lambda job_id, **kw: self.updates.update(kw),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for job_id in self.job_ids:
            shutil.rmtree(
                Path("/tmp") / f"waqqad_{job_id}",
                ignore_errors=True,
            )

    def _input_file(self, job_id, name, size):
        self.job_ids.append(job_id)
        original = Path("/tmp") / f"waqqad_{job_id}" / "original"
        original.mkdir(parents=True, exist_ok=True)
        path = original / name
        path.write_bytes(b"x" * size)
        return path

    def test_small_supported_file_is_sent_directly(self):
        path = self._input_file("test-direct", "a.mp3", 1000)

        with mock.patch.object(
            app.subprocess, "run",
            side_effect=AssertionError("ffmpeg must not run"),
        ):
            parts = app.prepare_cohere_parts("test-direct", path)

        self.assertEqual(parts, [path])
        self.assertEqual(self.updates["prepare_mode"], "direct")
        self.assertEqual(self.updates["total_parts"], 1)

    def test_large_mp3_is_split_without_reencoding(self):
        path = self._input_file(
            "test-copy", "a.mp3", app.COHERE_MAX_FILE_BYTES + 100
        )
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "ffmpeg":
                outdir = Path(cmd[-1]).parent
                (outdir / "part_000.mp3").write_bytes(b"y" * 1000)
                (outdir / "part_001.mp3").write_bytes(b"y" * 1000)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            app, "_probe_audio_duration", return_value=7200.0
        ):
            with mock.patch.object(app.subprocess, "run", fake_run):
                parts = app.prepare_cohere_parts("test-copy", path)

        self.assertEqual(
            [p.name for p in parts],
            ["part_000.mp3", "part_001.mp3"],
        )
        self.assertIn("copy", calls[0])
        self.assertEqual(self.updates["prepare_mode"], "copy")
        self.assertEqual(self.updates["total_parts"], 2)

    def test_oversized_copy_falls_back_to_reencode(self):
        path = self._input_file(
            "test-fallback", "a.mp3", app.COHERE_MAX_FILE_BYTES + 100
        )
        ffmpeg_calls = []

        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffmpeg":
                ffmpeg_calls.append(cmd)
                outdir = Path(cmd[-1]).parent
                if "copy" in cmd:
                    (outdir / "part_000.mp3").write_bytes(
                        b"z" * (app.COHERE_MAX_FILE_BYTES + 1)
                    )
                else:
                    (outdir / "part_000.mp3").write_bytes(b"a" * 1000)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(
            app, "_probe_audio_duration", return_value=7200.0
        ):
            with mock.patch.object(app.subprocess, "run", fake_run):
                parts = app.prepare_cohere_parts("test-fallback", path)

        self.assertEqual(len(ffmpeg_calls), 2)
        self.assertEqual([p.name for p in parts], ["part_000.mp3"])
        self.assertEqual(self.updates["prepare_mode"], "reencode")

    def test_audio_content_types(self):
        self.assertEqual(app._audio_content_type(Path("a.mp3")),
                         "audio/mpeg")
        self.assertEqual(app._audio_content_type(Path("a.wav")),
                         "audio/wav")
        self.assertEqual(app._audio_content_type(Path("a.ogg")),
                         "audio/ogg")
        self.assertEqual(app._audio_content_type(Path("a.flac")),
                         "audio/flac")


if __name__ == "__main__":
    unittest.main()
