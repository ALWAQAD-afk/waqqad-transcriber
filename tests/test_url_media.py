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

    def test_forced_pot_uses_always_and_skips_webpage(self):
        with TemporaryDirectory() as temp:
            options = url_media._youtube_options(
                Path(temp), 1024, None, ("mweb",),
                missing_pot=True, pot=True, force_pot=True,
            )
            youtube = options["extractor_args"]["youtube"]
            self.assertEqual(youtube["fetch_pot"], ["always"])
            self.assertEqual(youtube["pot_trace"], ["true"])
            self.assertEqual(youtube["player_skip"], ["webpage"])

    def test_non_forced_pot_leaves_policy_auto(self):
        with TemporaryDirectory() as temp:
            options = url_media._youtube_options(
                Path(temp), 1024, None, ("web",),
                missing_pot=True, pot=True,
            )
            youtube = options["extractor_args"]["youtube"]
            self.assertNotIn("fetch_pot", youtube)
            self.assertNotIn("player_skip", youtube)


class TestPotProvider(unittest.TestCase):
    def setUp(self):
        url_media._POT_STATE_CACHE.update({"checked_at": 0.0, "value": None})

    def test_state_reports_missing_install(self):
        with TemporaryDirectory() as temp:
            with mock.patch.object(url_media, "POT_SERVER_HOME", temp):
                self.assertEqual(
                    url_media.pot_provider_state(force=True),
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
                    url_media.pot_provider_state(force=True)
                )

    def test_state_failure_is_cached(self):
        """الفحص البطيء لا يُعاد قبل انتهاء مدة التخزين المؤقت."""
        with TemporaryDirectory() as temp:
            with mock.patch.object(url_media, "POT_SERVER_HOME", temp):
                first = url_media.pot_provider_state(force=True)
                second = url_media.pot_provider_state()
        self.assertEqual(first, second)

    def test_player_response_failure_counts_as_block(self):
        """هذا الخطأ كان يتجاوز المحاولة البديلة سابقًا."""
        self.assertTrue(
            url_media._looks_like_bot_check(
                "[youtube] abc123: Failed to extract any player response;"
            )
        )

    @staticmethod
    def _install_fake_ytdlp(extract_info):
        """يزرع وحدة yt_dlp وهمية ويعيد قائمة الخيارات المستخدمة."""
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
                return extract_info(self.options, calls)

        fake_module = types.ModuleType("yt_dlp")
        fake_module.version = types.SimpleNamespace(__version__="2026.10.26")
        fake_module.YoutubeDL = FakeYDL
        return fake_module, calls

    def test_plan_always_contains_bare_attempt(self):
        """حتى لو جاءت الكوكيز من متغيّر بيئة تبقى محاولة بلا كوكيز."""
        cookies = Path("/tmp/cookies.txt")
        plan = url_media._youtube_attempt_plan(
            cookies, cookies_auth=False, pot_ready=False
        )
        self.assertTrue(any(use_cookies is None for _, use_cookies, _, _ in plan))
        self.assertTrue(any(use_cookies for _, use_cookies, _, _ in plan))
        # لا محاولة مكرّرة.
        keys = [
            (
                tuple(clients) if clients else None,
                bool(use_cookies),
                bool(use_pot),
                bool(force_pot),
            )
            for clients, use_cookies, use_pot, force_pot in plan
        ]
        self.assertEqual(len(keys), len(set(keys)))

    def test_plan_puts_pot_after_js_free_clients(self):
        plan = url_media._youtube_attempt_plan(
            Path("/tmp/cookies.txt"), cookies_auth=True, pot_ready=True
        )
        pot_positions = [
            index for index, item in enumerate(plan) if item[2]
        ]
        bare_positions = [
            index for index, item in enumerate(plan) if item[1] is None
        ]
        self.assertTrue(pot_positions)
        self.assertLess(min(bare_positions), min(pot_positions))

    def test_download_retries_until_audio_is_selected(self):
        """نجاح استخراج البيانات دون صوت لا يُعدّ نجاحًا."""
        cookies = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
        )

        def extract_info(options, calls):
            home = Path(options["paths"]["home"])
            if len(calls) == 1:
                (home / "lecture.mp3").write_bytes(b"no audio")
                return {"title": "ok", "formats": [], "acodec": "none"}
            (home / "final.mp3").write_bytes(b"audio")
            return {"title": "ok", "formats": [], "acodec": "opus"}

        fake_module, calls = self._install_fake_ytdlp(extract_info)

        with TemporaryDirectory() as temp:
            with mock.patch.dict("sys.modules", {"yt_dlp": fake_module}):
                with mock.patch.object(
                    url_media, "pot_provider_state", return_value="غير مثبّت"
                ):
                    with mock.patch.object(
                        url_media, "_file_has_audio", return_value=True
                    ):
                        _, size, _ = url_media._download_youtube(
                            "https://www.youtube.com/watch?v=abc",
                            Path(temp),
                            1000,
                            cookies_text=cookies,
                        )
            self.assertEqual(
                sorted(path.name for path in Path(temp).iterdir()),
                ["final.mp3"]
            )

        self.assertEqual(size, len(b"audio"))
        self.assertEqual(len(calls), 2)
        # المحاولة الأولى بلا كوكيز والثانية بالكوكيز.
        self.assertIsNone(calls[0].get("cookiefile"))
        self.assertIsNotNone(calls[1].get("cookiefile"))
        # التنزيل لا يدرج صيغًا تحتاج توكنًا غير متوفر.
        for options in calls:
            self.assertNotIn(
                "missing_pot",
                options.get("extractor_args", {}).get("youtube", {}).get(
                    "formats", []
                )
            )

    def test_download_logs_never_leak_cookie_values(self):
        import contextlib
        import io

        secret = "SECRET-COOKIE-VALUE-987"
        cookies = (
            "# Netscape HTTP Cookie File\n"
            f".youtube.com\tTRUE\t/\tTRUE\t0\tSID\t{secret}\n"
        )

        def extract_info(options, calls):
            raise RuntimeError(
                "[youtube] abc: Sign in to confirm you're not a bot"
            )

        fake_module, _ = self._install_fake_ytdlp(extract_info)
        output = io.StringIO()
        with TemporaryDirectory() as temp:
            with mock.patch.dict("sys.modules", {"yt_dlp": fake_module}):
                with mock.patch.object(
                    url_media, "pot_provider_state", return_value="غير مثبّت"
                ):
                    with contextlib.redirect_stdout(output):
                        with self.assertRaises(url_media.UrlDownloadError):
                            url_media._download_youtube(
                                "https://www.youtube.com/watch?v=abc",
                                Path(temp),
                                1000,
                                cookies_text=cookies,
                            )
        self.assertNotIn(secret, output.getvalue())

    def test_download_keeps_raw_audio_when_ffmpeg_missing(self):
        """لا نُهدر صوتًا نُزّل بنجاح لأن التحويل إلى mp3 تعذّر."""
        def extract_info(options, calls):
            if options.get("postprocessors"):
                raise RuntimeError(
                    "Postprocessing: ffprobe and ffmpeg not found. "
                    "Please install or provide the path using --ffmpeg-location"
                )
            home = Path(options["paths"]["home"])
            (home / "raw.m4a").write_bytes(b"audio")
            return {"title": "ok", "formats": [], "acodec": "mp4a"}

        fake_module, calls = self._install_fake_ytdlp(extract_info)

        with TemporaryDirectory() as temp:
            with mock.patch.dict("sys.modules", {"yt_dlp": fake_module}):
                with mock.patch.object(
                    url_media, "pot_provider_state", return_value="غير مثبّت"
                ):
                    with mock.patch.object(
                        url_media, "_file_has_audio", return_value=True
                    ):
                        _, size, _ = url_media._download_youtube(
                            "https://www.youtube.com/watch?v=abc",
                            Path(temp),
                            1000,
                        )

        self.assertEqual(size, len(b"audio"))
        self.assertEqual(len(calls), 2)
        self.assertIn("postprocessors", calls[0])
        self.assertNotIn("postprocessors", calls[1])

    def test_pot_attempts_used_when_js_free_clients_fail(self):
        """عند جاهزية المزوّد تُجرَّب صيغ Web + PO بدل التكرار بلا فائدة."""
        cookies = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n"
        )

        def extract_info(options, calls):
            if "youtubepot-bgutilscript" not in options.get(
                "extractor_args", {}
            ):
                raise RuntimeError(
                    "ERROR: [youtube] abc: Requested format is not available"
                )
            home = Path(options["paths"]["home"])
            (home / "final.mp3").write_bytes(b"audio")
            return {"title": "ok", "formats": [], "acodec": "opus"}

        fake_module, calls = self._install_fake_ytdlp(extract_info)

        with TemporaryDirectory() as temp:
            with mock.patch.dict("sys.modules", {"yt_dlp": fake_module}):
                with mock.patch.object(
                    url_media, "pot_provider_state", return_value="جاهز ✅"
                ):
                    with mock.patch.object(
                        url_media, "_file_has_audio", return_value=True
                    ):
                        url_media._download_youtube(
                            "https://www.youtube.com/watch?v=abc",
                            Path(temp),
                            1000,
                            cookies_text=cookies,
                        )

        self.assertTrue(calls)
        self.assertGreater(len(calls), 1)
        last = calls[-1]
        self.assertIn("youtubepot-bgutilscript", last["extractor_args"])
        youtube = last["extractor_args"]["youtube"]
        self.assertEqual(youtube["fetch_pot"], ["always"])
        self.assertEqual(youtube["player_skip"], ["webpage"])
        self.assertEqual(
            youtube["player_client"],
            list(url_media._youtube_clients(
                url_media.YOUTUBE_POT_CLIENTS[0]
            ))
        )
        # المحاولات السابقة كانت بلا PO.
        self.assertNotIn(
            "youtubepot-bgutilscript", calls[0]["extractor_args"]
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

    def test_cookie_names_state_lists_missing_keys(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tone\n"
            ".youtube.com\tTRUE\t/\tTRUE\t0\tHSID\ttwo\n"
        )
        state = url_media.cookie_names_state(text)
        self.assertIn("2/10", state)
        self.assertIn("__Secure-1PSID", state)

    def test_cookie_names_state_reports_complete(self):
        lines = ["# Netscape HTTP Cookie File"]
        for name in url_media.COOKIE_KEY_NAMES:
            lines.append(f".youtube.com\tTRUE\t/\tTRUE\t0\t{name}\tv")
        state = url_media.cookie_names_state("\n".join(lines))
        self.assertIn("10/10", state)
        self.assertIn("كاملة", state)

    def test_cookie_names_state_never_leaks_values(self):
        text = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-value\n"
        self.assertNotIn("secret-value", url_media.cookie_names_state(text))


class TestYoutubeClassification(unittest.TestCase):
    def test_classifies_auth_errors(self):
        self.assertEqual(
            url_media.classify_youtube_error(
                "ERROR: [youtube] abc: Sign in to confirm you're not a bot"
            ),
            "auth",
        )

    def test_classifies_player_errors(self):
        for message in (
            "Failed to extract any player response",
            "The page needs to be reloaded",
        ):
            self.assertEqual(
                url_media.classify_youtube_error(message), "player"
            )

    def test_classifies_format_and_fatal_and_download(self):
        self.assertEqual(
            url_media.classify_youtube_error(
                "Requested format is not available"
            ),
            "format",
        )
        self.assertEqual(
            url_media.classify_youtube_error("Video unavailable"), "fatal"
        )
        self.assertEqual(
            url_media.classify_youtube_error("HTTP Error 403: Forbidden"),
            "download",
        )

    def test_classifies_missing_ffmpeg(self):
        self.assertEqual(
            url_media.classify_youtube_error(
                "Postprocessing: ffprobe and ffmpeg not found"
            ),
            "ffmpeg",
        )
        error = url_media._youtube_failure(
            "Postprocessing: ffprobe and ffmpeg not found"
        )
        self.assertIn("ffmpeg", str(error))

    def test_redacts_sensitive_query_values(self):
        message = (
            "https://x/y?signature=SECRET123&token=SECRET456&pot=SECRET789"
            "&expire=123&id=ok /pot/SECRETPOT"
        )
        redacted = url_media._redact_media_urls(message)
        for secret in ("SECRET123", "SECRET456", "SECRET789", "SECRETPOT"):
            self.assertNotIn(secret, redacted)
        self.assertIn("id=ok", redacted)

    def test_short_reason_strips_prefix_and_redacts(self):
        reason = url_media._short_reason(
            "ERROR: [youtube] abc: failed https://x/y?token=SECRET"
        )
        self.assertFalse(reason.startswith("ERROR"))
        self.assertNotIn("SECRET", reason)


class TestFormatsClassification(unittest.TestCase):
    def test_counts_only_usable_audio(self):
        info = {
            "formats": [
                {"format_id": "sb0", "acodec": "none", "vcodec": "none"},
                {"format_id": "251", "acodec": "opus", "vcodec": "none"},
                {
                    "format_id": "139",
                    "acodec": "mp4a",
                    "vcodec": "none",
                    "format_note": "MISSING POT",
                },
                {
                    "format_id": "140-drc",
                    "acodec": "mp4a",
                    "vcodec": "none",
                    "has_drm": "maybe",
                },
                {
                    "format_id": "22",
                    "acodec": "mp4a",
                    "vcodec": "avc1",
                },
            ]
        }
        summary = url_media.youtube_formats_summary(info)
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["audio"], 2)
        self.assertEqual(summary["missing_pot"], 1)
        self.assertEqual(summary["drm"], 1)
        self.assertEqual(
            [fmt["format_id"] for fmt in summary["formats"]],
            ["251", "22"],
        )

    def test_combined_video_audio_counts_as_audio(self):
        info = {"formats": [
            {"format_id": "18", "acodec": "mp4a", "vcodec": "avc1"}
        ]}
        self.assertEqual(len(url_media.usable_audio_formats(info)), 1)

    def test_info_has_audio_checks_requested_formats(self):
        self.assertFalse(url_media._info_has_audio({"acodec": "none"}))
        self.assertTrue(url_media._info_has_audio({"acodec": "opus"}))
        self.assertFalse(url_media._info_has_audio({
            "requested_formats": [
                {"acodec": "none"}, {"acodec": "none"}
            ]
        }))
        self.assertTrue(url_media._info_has_audio({
            "requested_formats": [
                {"acodec": "none"}, {"acodec": "opus"}
            ]
        }))

    def test_cleanup_removes_only_new_files(self):
        with TemporaryDirectory() as temp:
            home = Path(temp)
            (home / "old.mp3").write_bytes(b"old")
            before = url_media._attempt_files(home)
            (home / "new.mp3").write_bytes(b"new")
            url_media._cleanup_attempt_files(home, before)
            self.assertEqual(
                sorted(path.name for path in home.iterdir()), ["old.mp3"]
            )


class TestCookieAuthState(unittest.TestCase):
    @staticmethod
    def _cookie_text(**values):
        lines = ["# Netscape HTTP Cookie File"]
        for name, value in values.items():
            lines.append(f".youtube.com\tTRUE\t/\tTRUE\t0\t{name}\t{value}")
        return "\n".join(lines)

    def test_requires_login_info_and_sapisid(self):
        full = self._cookie_text(LOGIN_INFO="a", SAPISID="b")
        self.assertIn("موثّقة", url_media.cookies_auth_state(full))
        self.assertTrue(
            url_media._cookies_authenticated(
                url_media._parse_netscape_cookies(full)
            )
        )

    def test_missing_login_info_is_reported_without_values(self):
        text = self._cookie_text(SAPISID="SECRET-SAPISID-VALUE")
        state = url_media.cookies_auth_state(text)
        self.assertIn("غير موثّقة", state)
        self.assertIn("LOGIN_INFO", state)
        self.assertNotIn("SECRET-SAPISID-VALUE", state)

    def test_secure_3papisid_is_accepted(self):
        text = self._cookie_text(
            LOGIN_INFO="a", **{"__Secure-3PAPISID": "b"}
        )
        self.assertIn("موثّقة", url_media.cookies_auth_state(text))

    def test_ejs_state_is_reported(self):
        self.assertTrue(url_media.ejs_solver_state())


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
