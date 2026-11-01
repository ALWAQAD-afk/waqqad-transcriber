import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import bot_upload


class TestExtractAttachment(unittest.TestCase):
    def test_document(self):
        message = {
            "document": {
                "file_id": "abc",
                "file_unique_id": "u1",
                "file_name": "lecture.mp3",
                "file_size": 1024,
                "mime_type": "audio/mpeg",
            }
        }
        attachment = bot_upload.extract_attachment(message)
        self.assertEqual(attachment["kind"], "document")
        self.assertEqual(attachment["file_name"], "lecture.mp3")
        self.assertTrue(bot_upload.is_supported(attachment))

    def test_voice_without_name_uses_default(self):
        attachment = bot_upload.extract_attachment(
            {"voice": {"file_id": "v1", "duration": 12}}
        )
        self.assertEqual(attachment["kind"], "voice")
        self.assertEqual(attachment["file_name"], "voice.ogg")
        self.assertTrue(bot_upload.is_supported(attachment))

    def test_text_message_has_no_attachment(self):
        self.assertIsNone(bot_upload.extract_attachment({"text": "مرحبًا"}))

    def test_photo_is_not_an_attachment(self):
        self.assertIsNone(
            bot_upload.extract_attachment({"photo": [{"file_id": "p1"}]})
        )


class TestSupportedFiles(unittest.TestCase):
    def test_audio_mime_is_supported(self):
        self.assertTrue(bot_upload.is_supported({
            "file_id": "1",
            "file_name": "x.bin",
            "mime_type": "audio/mpeg",
            "kind": "document",
        }))

    def test_mp3_extension_without_mime(self):
        self.assertTrue(bot_upload.is_supported({
            "file_id": "1",
            "file_name": "Lecture.MP3",
            "mime_type": None,
            "kind": "document",
        }))

    def test_pdf_is_rejected(self):
        self.assertFalse(bot_upload.is_supported({
            "file_id": "1",
            "file_name": "notes.pdf",
            "mime_type": "application/pdf",
            "kind": "document",
        }))

    def test_attachment_without_file_id_is_rejected(self):
        self.assertFalse(bot_upload.is_supported({"file_name": "a.mp3"}))


class TestHumanSize(unittest.TestCase):
    def test_megabytes(self):
        self.assertEqual(bot_upload.human_size(14 * 1024 * 1024), "14.0 MB")

    def test_bytes(self):
        self.assertEqual(bot_upload.human_size(512), "512 بايت")


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, chunks=None):
        self.status_code = status_code
        self._payload = payload or {}
        self._chunks = chunks or []
        self.content = b"{}"

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=0):
        for chunk in self._chunks:
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, get_file, stream):
        self._get_file = get_file
        self._stream = stream

    def get(self, url, params=None, timeout=None, stream=False):
        if "getFile" in url:
            return self._get_file
        return self._stream

    def close(self):
        return None


class TestDownload(unittest.TestCase):
    def _session(self, size, chunks):
        get_file = _FakeResponse(payload={
            "ok": True,
            "result": {"file_path": "voice/file.ogg", "file_size": size},
        })
        stream = _FakeResponse(chunks=chunks)
        return _FakeSession(get_file, stream)

    def test_downloads_and_returns_size(self):
        with TemporaryDirectory() as tmp, mock.patch(
            "bot_upload.requests.Session",
            return_value=self._session(6, [b"abc", b"def"]),
        ):
            destination = Path(tmp) / "voice.ogg"
            written = bot_upload.download_telegram_file(
                "token", "file-id", destination, max_bytes=100
            )
            self.assertEqual(written, 6)
            self.assertEqual(destination.read_bytes(), b"abcdef")

    def test_reported_size_over_limit_raises_before_download(self):
        with TemporaryDirectory() as tmp, mock.patch(
            "bot_upload.requests.Session",
            return_value=self._session(500, [b"x"]),
        ):
            destination = Path(tmp) / "big.mp3"
            with self.assertRaises(bot_upload.FileTooLarge):
                bot_upload.download_telegram_file(
                    "token", "file-id", destination, max_bytes=100
                )
            self.assertFalse(destination.exists())

    def test_stream_over_limit_stops_and_cleans_up(self):
        with TemporaryDirectory() as tmp, mock.patch(
            "bot_upload.requests.Session",
            return_value=self._session(0, [b"x" * 80, b"y" * 80]),
        ):
            destination = Path(tmp) / "sneaky.mp3"
            with self.assertRaises(bot_upload.FileTooLarge):
                bot_upload.download_telegram_file(
                    "token", "file-id", destination, max_bytes=100
                )
            self.assertFalse(destination.exists())

    def test_get_file_failure_raises_download_failed(self):
        with TemporaryDirectory() as tmp, mock.patch(
            "bot_upload.requests.Session",
            return_value=self._session(0, [b""]),
        ):
            destination = Path(tmp) / "x.mp3"
            with self.assertRaises(bot_upload.DownloadFailed):
                bot_upload.download_telegram_file(
                    "token", "file-id", destination, max_bytes=100
                )


if __name__ == "__main__":
    unittest.main()
