import base64
import os
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


class TestYoutubeErrors(unittest.TestCase):
    def test_bot_check_has_clear_message_and_detail(self):
        error = url_media._youtube_failure(
            "ERROR: [youtube] abc: Sign in to confirm you're not a bot. "
            "Use --cookies-from-browser or --cookies for the authentication."
        )
        self.assertIsInstance(error, url_media.UrlDownloadError)
        self.assertIn("فحص تحقق", str(error))
        self.assertIn("not a bot", error.detail)

    def test_unavailable_video_is_explained(self):
        error = url_media._youtube_failure(
            "ERROR: [youtube] abc: Video unavailable"
        )
        self.assertIn("غير متاح", str(error))

    def test_oversize_stays_oversize(self):
        error = url_media._youtube_failure(
            "ERROR: File is larger than max-filesize"
        )
        self.assertIsInstance(error, url_media.UrlTooLarge)

    def test_no_playable_format_is_explained_as_server_side(self):
        """يوتيوب قد يُرجع الاستخراج بلا صيغ صوتية من عنوان الخادم."""
        error = url_media._youtube_failure(
            "ERROR: [youtube] abc: Requested format is not available. "
            "Use --list-formats for a list of available formats"
        )
        self.assertIsInstance(error, url_media.UrlDownloadError)
        self.assertIn("بيئة الخادم", str(error))
        self.assertIn("Requested format", error.detail)

    def test_short_reason_is_one_line(self):
        reason = url_media._short_reason(
            "ERROR: first line\nsecond line   with spaces"
        )
        self.assertEqual(reason, "first line second line with spaces")


class TestCookiesText(unittest.TestCase):
    def test_valid_youtube_cookies(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
            "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tHSID\tvalue\n"
        )
        self.assertEqual(url_media.validate_cookies_text(text), 2)

    def test_rejects_non_cookie_file(self):
        with self.assertRaises(url_media.UrlDownloadError):
            url_media.validate_cookies_text("<html>not cookies</html>")

    def test_rejects_cookies_without_youtube(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".example.com\tTRUE\t/\tTRUE\t0\tk\tv\n"
        )
        with self.assertRaises(url_media.UrlDownloadError):
            url_media.validate_cookies_text(text)

    def test_rejects_empty(self):
        with self.assertRaises(url_media.UrlDownloadError):
            url_media.validate_cookies_text("")

    def test_cookies_text_is_written_for_job(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
        )
        with TemporaryDirectory() as temp:
            path, temporary = url_media._youtube_cookies_file(
                Path(temp), text
            )
            self.assertTrue(temporary)
            self.assertEqual(path.read_text(encoding="utf-8"), text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class TestYoutubeCookies(unittest.TestCase):
    def test_missing_cookies_returns_none(self):
        with mock.patch.dict(
            os.environ,
            {"YTDLP_COOKIES_FILE": "", "YTDLP_COOKIES_B64": ""},
            clear=False,
        ):
            with TemporaryDirectory() as temp:
                self.assertEqual(
                    url_media._youtube_cookies_file(Path(temp)),
                    (None, False)
                )

    def test_base64_cookies_are_written(self):
        raw = b"# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t0\tk\tv\n"
        encoded = base64.b64encode(raw).decode()
        with mock.patch.dict(
            os.environ,
            {"YTDLP_COOKIES_FILE": "", "YTDLP_COOKIES_B64": encoded},
            clear=False,
        ):
            with TemporaryDirectory() as temp:
                path, temporary = url_media._youtube_cookies_file(Path(temp))
                self.assertTrue(temporary)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_base64_is_rejected(self):
        with mock.patch.dict(
            os.environ,
            {"YTDLP_COOKIES_FILE": "", "YTDLP_COOKIES_B64": "not base64!!"},
            clear=False,
        ):
            with TemporaryDirectory() as temp:
                with self.assertRaises(url_media.UrlDownloadError):
                    url_media._youtube_cookies_file(Path(temp))

    def test_options_include_alternate_client(self):
        with TemporaryDirectory() as temp:
            options = url_media._youtube_options(
                Path(temp), 1024, None, ("tv", "android_vr"),
                missing_pot=True,
            )
            self.assertEqual(
                options["extractor_args"]["youtube"]["player_client"],
                ["tv", "android_vr"]
            )
            # الصيغ التي تحتاج PO Token تُدرج عند طلبها.
            self.assertIn(
                "missing_pot",
                options["extractor_args"]["youtube"]["formats"]
            )

    def test_options_include_missing_pot_without_clients(self):
        with TemporaryDirectory() as temp:
            options = url_media._youtube_options(
                Path(temp), 1024, None, missing_pot=True
            )
            self.assertEqual(
                options["extractor_args"]["youtube"]["formats"],
                ["missing_pot"]
            )

    def test_options_omit_missing_pot_by_default(self):
        with TemporaryDirectory() as temp:
            options = url_media._youtube_options(Path(temp), 1024, None)
            self.assertNotIn("extractor_args", options)

    def test_options_request_pot_provider(self):
        with TemporaryDirectory() as temp:
            options = url_media._youtube_options(
                Path(temp), 1024, None, ("web",),
                missing_pot=True, pot=True,
            )
            self.assertEqual(
                options["extractor_args"]["youtubepot-bgutilscript"][
                    "server_home"
                ],
                [url_media.POT_SERVER_HOME]
            )


class TestPotProvider(unittest.TestCase):
    def test_state_reports_missing_install(self):
        with TemporaryDirectory() as temp:
            with mock.patch.object(url_media, "POT_SERVER_HOME", temp):
                self.assertEqual(
                    url_media.pot_provider_state(),
                    "غير مثبّت"
                )

    def test_state_reports_missing_node_modules(self):
        with TemporaryDirectory() as temp:
            src = Path(temp) / "src"
            src.mkdir()
            (src / "generate_once.ts").write_text("// x", encoding="utf-8")
            with mock.patch.object(url_media, "POT_SERVER_HOME", temp):
                self.assertIn(
                    "node_modules",
                    url_media.pot_provider_state()
                )

    def test_player_response_failure_counts_as_block(self):
        """هذا الخطأ كان يتجاوز المحاولة البديلة سابقًا."""
        self.assertTrue(
            url_media._looks_like_bot_check(
                "[youtube] abc123: Failed to extract any player response;"
            )
        )

    def test_bare_attempt_runs_without_cookies_before_giving_up(self):
        """إن فشلت كل محاولات الكوكيز تُجرَّب محاولة أخيرة بلا كوكيز."""
        import sys
        import types

        sys.modules.pop("yt_dlp", None)

        calls = []

        class FakeYDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                calls.append(self.options)
                if self.options.get("cookiefile"):
                    raise RuntimeError(
                        "[youtube] abc: Sign in to confirm you're not a bot"
                    )
                home = Path(self.options["paths"]["home"])
                (home / "lecture.mp3").write_bytes(b"audio")
                return {"title": "ok", "formats": []}

        fake_module = types.ModuleType("yt_dlp")
        fake_module.version = types.SimpleNamespace(__version__="2026.10.26")
        fake_module.YoutubeDL = FakeYDL

        cookies = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
        )

        with TemporaryDirectory() as temp:
            with mock.patch.dict(sys.modules, {"yt_dlp": fake_module}):
                with mock.patch.object(
                    url_media, "pot_provider_state", return_value="غير مثبّت"
                ):
                    info = url_media._download_youtube(
                        "https://www.youtube.com/watch?v=abc",
                        Path(temp),
                        1000,
                        cookies_text=cookies,
                    )

        self.assertEqual(info[2], "ok")
        # أربع محاولات بالكوكيز ثم محاولة أخيرة بلا كوكيز.
        self.assertEqual(len(calls), 5)
        self.assertIsNone(calls[-1].get("cookiefile"))
        self.assertEqual(
            calls[-1]["extractor_args"]["youtube"]["player_client"],
            list(url_media.YOUTUBE_BARE_CLIENTS)
        )
        # المحاولة الأولى طبيعية، والثانية تُدرج صيغ missing PO Token.
        self.assertNotIn("extractor_args", calls[0])
        self.assertIn(
            "missing_pot",
            calls[1]["extractor_args"]["youtube"]["formats"]
        )

    def test_pot_attempts_run_first_when_provider_ready(self):
        """عند توفّر المزوّد نبدأ بمحاولة تحمل توكن إثبات الأصل."""
        import sys
        import types

        sys.modules.pop("yt_dlp", None)

        calls = []

        class FakeYDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=True):
                calls.append(self.options)
                home = Path(self.options["paths"]["home"])
                (home / "lecture.mp3").write_bytes(b"audio")
                return {"title": "ok", "formats": []}

        fake_module = types.ModuleType("yt_dlp")
        fake_module.version = types.SimpleNamespace(__version__="2026.10.26")
        fake_module.YoutubeDL = FakeYDL

        cookies = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
        )

        with TemporaryDirectory() as temp:
            with mock.patch.dict(sys.modules, {"yt_dlp": fake_module}):
                with mock.patch.object(
                    url_media, "pot_provider_state", return_value="جاهز ✅"
                ):
                    info = url_media._download_youtube(
                        "https://www.youtube.com/watch?v=abc",
                        Path(temp),
                        1000,
                        cookies_text=cookies,
                    )

        self.assertEqual(info[2], "ok")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["extractor_args"]["youtubepot-bgutilscript"][
                "server_home"
            ],
            [url_media.POT_SERVER_HOME]
        )
        self.assertIn(
            "missing_pot",
            calls[0]["extractor_args"]["youtube"]["formats"]
        )


class TestCookieStatus(unittest.TestCase):
    def test_parses_netscape_cookies(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tone\n"
            "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tHSID\ttwo\n"
        )
        self.assertEqual(
            url_media._parse_netscape_cookies(text),
            {"SID": "one", "HSID": "two"}
        )

    def test_detects_logged_in_html(self):
        self.assertTrue(
            url_media._cookie_login_state_from_html(
                'ytcfg.set({"LOGGED_IN":true,"X":1});'
            )
        )
        self.assertTrue(
            url_media._cookie_login_state_from_html(
                '"LOGGED_IN" : true'
            )
        )

    def test_detects_logged_out_html(self):
        self.assertFalse(
            url_media._cookie_login_state_from_html(
                '{"LOGGED_IN":false}'
            )
        )

    def test_unknown_html_returns_none(self):
        self.assertIsNone(
            url_media._cookie_login_state_from_html("<html></html>")
        )

    def test_status_without_cookies(self):
        self.assertIn(
            "لا توجد كوكيز",
            url_media.youtube_cookie_status("")
        )


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
