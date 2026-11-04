import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import url_media


class FakeResponse:
    def __init__(self, headers=None, chunks=None, status=200):
        self.headers = headers or {}
        self._chunks = chunks or []
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size=1):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


class TestUrlParsing(unittest.TestCase):
    def test_valid_url(self):
        self.assertTrue(url_media.is_url("https://example.com/a.mp3"))

    def test_rejects_non_http(self):
        self.assertFalse(url_media.is_url("file:///etc/passwd"))
        self.assertFalse(url_media.is_url("javascript:alert(1)"))

    def test_rejects_spaces(self):
        self.assertFalse(url_media.is_url("https://example.com/a b.mp3"))

    def test_extracts_url_from_text(self):
        self.assertEqual(
            url_media.extract_url("فرّغ هذا: https://youtu.be/abc123 شكرًا"),
            "https://youtu.be/abc123"
        )

    def test_no_url(self):
        self.assertIsNone(url_media.extract_url("مرحبًا بكم"))

    def test_youtube_detection(self):
        self.assertTrue(url_media.is_youtube("https://www.youtube.com/watch?v=1"))
        self.assertTrue(url_media.is_youtube("https://youtu.be/abc"))
        self.assertFalse(url_media.is_youtube("https://example.com/a.mp3"))


class TestSecurity(unittest.TestCase):
    def test_localhost_is_rejected(self):
        self.assertFalse(url_media._is_public_host("localhost"))
        self.assertFalse(url_media._is_public_host("127.0.0.1"))

    def test_private_ip_is_rejected(self):
        self.assertFalse(url_media._is_public_host("192.168.1.10"))

    def test_safe_filename_keeps_arabic(self):
        self.assertEqual(
            url_media._safe_filename("محاضرة الأصول.mp3"),
            "محاضرة الأصول.mp3"
        )

    def test_safe_filename_removes_path(self):
        self.assertEqual(
            url_media._safe_filename("../../etc/passwd"),
            "passwd"
        )


class TestDirectDownload(unittest.TestCase):
    def test_downloads_audio_and_returns_size(self):
        response = FakeResponse(
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Length": "8",
            },
            chunks=[b"1234", b"5678"],
        )
        with TemporaryDirectory() as temp:
            with mock.patch.object(
                url_media, "_open_stream",
                return_value=(response, "https://example.com/lecture.mp3")
            ):
                path, size, name = url_media._download_direct(
                    "https://example.com/lecture.mp3",
                    Path(temp),
                    1024
                )
                self.assertEqual(size, 8)
                self.assertEqual(name, "lecture.mp3")
                self.assertEqual(path.read_bytes(), b"12345678")
        self.assertTrue(response.closed)

    def test_rejects_html_page(self):
        response = FakeResponse(
            headers={"Content-Type": "text/html"},
            chunks=[b"<html>"],
        )
        with TemporaryDirectory() as temp:
            with mock.patch.object(
                url_media, "_open_stream",
                return_value=(response, "https://example.com/page")
            ):
                with self.assertRaises(url_media.UnsupportedUrl):
                    url_media._download_direct(
                        "https://example.com/page",
                        Path(temp),
                        1024
                    )

    def test_rejects_declared_oversize(self):
        response = FakeResponse(
            headers={
                "Content-Type": "audio/mpeg",
                "Content-Length": "5000",
            }
        )
        with TemporaryDirectory() as temp:
            with mock.patch.object(
                url_media, "_open_stream",
                return_value=(response, "https://example.com/a.mp3")
            ):
                with self.assertRaises(url_media.UrlTooLarge):
                    url_media._download_direct(
                        "https://example.com/a.mp3",
                        Path(temp),
                        1024
                    )

    def test_enforces_size_while_streaming(self):
        response = FakeResponse(
            headers={"Content-Type": "audio/mpeg"},
            chunks=[b"a" * 600, b"b" * 600],
        )
        with TemporaryDirectory() as temp:
            with mock.patch.object(
                url_media, "_open_stream",
                return_value=(response, "https://example.com/a.mp3")
            ):
                with self.assertRaises(url_media.UrlTooLarge):
                    url_media._download_direct(
                        "https://example.com/a.mp3",
                        Path(temp),
                        1000
                    )
                self.assertEqual(list(Path(temp).iterdir()), [])


class TestDisplayName(unittest.TestCase):
    def test_youtube_name(self):
        self.assertEqual(
            url_media.display_name_for_url("https://youtu.be/abc"),
            "رابط يوتيوب"
        )

    def test_file_name_from_url(self):
        self.assertEqual(
            url_media.display_name_for_url("https://example.com/lecture.mp3"),
            "lecture.mp3"
        )


if __name__ == "__main__":
    unittest.main()
