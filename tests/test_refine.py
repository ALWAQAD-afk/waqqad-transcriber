import re
import threading
import time
import unittest
from unittest import mock

import refine


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def prompt_text(payload):
    return payload["contents"][0]["parts"][0]["text"]


def extract_chunk(payload):
    match = re.search(r"---\n(.*?)\n---", prompt_text(payload), re.S)
    return match.group(1) if match else ""


def ok_payload(text):
    return {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": text}]},
        }]
    }


class RefineTests(unittest.TestCase):
    def setUp(self):
        self.original_key = refine.GEMINI_API_KEY
        refine.GEMINI_API_KEY = "test-key"
        self.patchers = [
            mock.patch.object(refine, "REFINE_REQUEST_GAP", 0),
            mock.patch.object(refine, "REFINE_MAX_RETRIES", 0),
            mock.patch.object(refine, "REFINE_CONCURRENCY", 1),
            mock.patch.object(refine.time, "sleep", lambda *_: None),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        refine.GEMINI_API_KEY = self.original_key
        for patcher in reversed(self.patchers):
            patcher.stop()

    # ---------- التقسيم ----------

    def test_split_preserves_all_characters(self):
        text = "\n\n".join(
            f"فقرة رقم {i} فيها جملة. وجملة أخرى؟ ثم ثالثة!"
            for i in range(80)
        )
        chunks = refine.split_text_smart(text, max_chars=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 300 for c in chunks))
        compact = lambda s: re.sub(r"\s+", "", s)
        self.assertEqual(compact("".join(chunks)), compact(text))

    def test_long_paragraph_is_split(self):
        paragraph = "هذه جملة طويلة للاختبار. " * 400
        chunks = refine.split_text_smart(paragraph, max_chars=500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        compact = lambda s: re.sub(r"\s+", "", s)
        self.assertEqual(compact("".join(chunks)), compact(paragraph))

    def test_no_text_is_lost_on_hard_split(self):
        text = "ا" * 5000
        chunks = refine.split_text_smart(text, max_chars=500)
        self.assertEqual("".join(chunks), text)

    # ---------- معالجة أخطاء الـAPI ----------

    def test_rejected_long_chunk_is_split_and_retried(self):
        def fake_post(url, headers, json, timeout):
            chunk = extract_chunk(json)
            if len(chunk) > 900:
                return FakeResponse(
                    400,
                    text="The input token count exceeds the maximum limit",
                )
            return FakeResponse(200, ok_payload(chunk))

        text = ("جملة عربية للاختبار. " * 120).strip()
        with mock.patch.object(refine.requests, "post", fake_post):
            result = refine.refine_document(text, "conservative")

        self.assertEqual(result["fallbacks"], 0)
        self.assertEqual(re.sub(r"\s+", "", result["text"]),
                         re.sub(r"\s+", "", text))

    def test_truncated_response_is_split_and_retried(self):
        def fake_post(url, headers, json, timeout):
            chunk = extract_chunk(json)
            if len(chunk) > 700:
                return FakeResponse(200, {
                    "candidates": [{"finishReason": "MAX_TOKENS"}]
                })
            return FakeResponse(200, ok_payload(chunk))

        text = ("عبارة للاختبار. " * 150).strip()
        with mock.patch.object(refine.requests, "post", fake_post):
            result = refine.refine_document(text, "conservative")

        self.assertEqual(result["fallbacks"], 0)
        self.assertEqual(re.sub(r"\s+", "", result["text"]),
                         re.sub(r"\s+", "", text))

    def test_rate_limit_is_retried(self):
        calls = {"n": 0}

        def fake_post(url, headers, json, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(
                    429, text="rate limited",
                    headers={"Retry-After": "0"},
                )
            return FakeResponse(200, ok_payload(extract_chunk(json)))

        with mock.patch.object(refine, "REFINE_MAX_RETRIES", 1):
            with mock.patch.object(refine.requests, "post", fake_post):
                text = "نص قصير للاختبار."
                result = refine.refine_document(text, "conservative")

        self.assertEqual(calls["n"], 2)
        self.assertEqual(result["text"], text)

    def test_unsupported_thinking_config_is_retried_without_it(self):
        calls = {"n": 0}

        def fake_post(url, headers, json, timeout):
            calls["n"] += 1
            config = json["generationConfig"]
            if "thinkingConfig" in config:
                return FakeResponse(
                    400,
                    text='Unknown name "thinkingConfig" at generationConfig',
                )
            return FakeResponse(200, ok_payload(extract_chunk(json)))

        text = "نص قصير للاختبار."
        with mock.patch.object(refine.requests, "post", fake_post):
            result = refine.refine_document(text, "conservative")

        self.assertEqual(calls["n"], 2)
        self.assertEqual(result["text"], text)

    def test_invalid_key_in_400_is_fatal(self):
        def fake_post(url, headers, json, timeout):
            return FakeResponse(
                400,
                text='{"error": {"status": "INVALID_ARGUMENT", '
                     '"message": "API key not valid. Please pass a valid API key."}}',
            )

        with mock.patch.object(refine.requests, "post", fake_post):
            with self.assertRaises(refine.GeminiFatalError):
                refine.refine_document("نص للاختبار.", "conservative")

    def test_missing_model_in_400_is_fatal(self):
        def fake_post(url, headers, json, timeout):
            return FakeResponse(
                400,
                text="models/gemini-x is not found for API version v1beta",
            )

        with mock.patch.object(refine.requests, "post", fake_post):
            with self.assertRaises(refine.GeminiFatalError):
                refine.refine_document("نص للاختبار.", "conservative")

    def test_blocked_chunk_falls_back_to_original(self):
        def fake_post(url, headers, json, timeout):
            return FakeResponse(200, {
                "promptFeedback": {"blockReason": "SAFETY"},
                "candidates": [],
            })

        text = "نص محاضرة عربي للاختبار."
        with mock.patch.object(refine.requests, "post", fake_post):
            result = refine.refine_document(text, "conservative")

        self.assertEqual(result["text"], text)
        self.assertEqual(result["fallbacks"], 1)
        self.assertTrue(result["issues"])

    def test_hallucination_is_rejected(self):
        def fake_post(url, headers, json, timeout):
            return FakeResponse(
                200, ok_payload("كلام مختلف تماماً " * 80)
            )

        text = "النص الأصلي للمحاضرة يجب أن يبقى كما هو."
        with mock.patch.object(refine.requests, "post", fake_post):
            result = refine.refine_document(text, "conservative")

        self.assertEqual(result["text"], text)
        self.assertEqual(result["fallbacks"], 1)

    def test_quota_exhaustion_is_fatal(self):
        def fake_post(url, headers, json, timeout):
            return FakeResponse(429, text="quota")

        with mock.patch.object(refine, "REFINE_MAX_RETRIES", 0):
            with mock.patch.object(refine.requests, "post", fake_post):
                with self.assertRaises(refine.GeminiQuotaError):
                    refine.refine_document("نص للاختبار.", "conservative")

    def test_missing_key_is_fatal(self):
        refine.GEMINI_API_KEY = ""
        with self.assertRaises(refine.GeminiFatalError):
            refine.refine_document("نص للاختبار.", "conservative")

    # ---------- تنظيف المخرجات ----------

    def test_clean_output_removes_fences_and_label(self):
        raw = "```text\nالنص المصحح:\nهذا نص سليم.\n```"
        self.assertEqual(refine._clean_output(raw), "هذا نص سليم.")

    # ---------- حماية المحتوى ----------

    def test_changed_number_is_rejected(self):
        src = "وصلنا إلى 43 بالمئة تقريباً."
        out = "وصلنا إلى 1043 بالمئة تقريباً."
        self.assertFalse(refine._is_valid(src, out, "liberal"))

    def test_added_latin_name_is_rejected(self):
        src = "شغلت اللعبة على الجهاز."
        out = "شغلت اللعبة على الجهاز باستخدام shadPS4."
        self.assertFalse(refine._is_valid(src, out, "liberal"))

    def test_arabic_indic_digits_match_western_digits(self):
        self.assertTrue(refine._preserves_content("سنة ١٩٩٩", "سنة 1999"))

    def test_number_separators_are_ignored(self):
        self.assertTrue(refine._preserves_content("200,000", "200000"))

    def test_persian_leakage_is_repaired(self):
        src = "خذ الأدوات حقتي."
        out = "خذ الأدوات حقتی."
        repaired = refine._repair_common_chars(src, out)
        self.assertNotIn("ی", repaired)
        self.assertTrue(refine._is_valid(src, repaired, "conservative"))

    def test_persian_letter_is_rejected(self):
        src = "خذ الأدوات حقتي."
        out = "خذ الأدوات حقتی."
        self.assertFalse(refine._is_valid(src, out, "conservative"))

    # ---------- الشعر ----------

    def test_poetry_cue_selects_poetry_prompt(self):
        prompts = []

        def fake_post(url, headers, json, timeout):
            prompts.append(prompt_text(json))
            return FakeResponse(200, ok_payload(extract_chunk(json)))

        text = "قال الشاعر:\nقفا نبك من ذكرى حبيب ومنزل\nبسقط اللوى بين الدخول فحومل"
        with mock.patch.object(refine.requests, "post", fake_post):
            result = refine.refine_document(text, "liberal")

        self.assertIn("ناسخ أمين", prompts[0])
        self.assertEqual(result["text"], text)

    # ---------- التوازي ----------

    def test_parallel_chunks_preserve_order(self):
        blocks = [
            f"الفقرة رقم {i}. " + ("هذه جملة للاختبار. " * 60)
            for i in range(1, 7)
        ]
        text = "\n\n".join(blocks)
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def fake_post(url, headers, json, timeout):
            chunk = extract_chunk(json)
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            threading.Event().wait(0.03)
            with lock:
                state["active"] -= 1
            return FakeResponse(200, ok_payload(chunk))

        with mock.patch.object(refine, "REFINE_CONCURRENCY", 3):
            with mock.patch.object(refine.requests, "post", fake_post):
                result = refine.refine_document(text, "conservative")

        def compact(value):
            return re.sub(r"\s+", "", value)

        self.assertGreaterEqual(state["peak"], 2)
        self.assertEqual(compact(result["text"]), compact(text))
        self.assertEqual(result["fallbacks"], 0)

    def test_output_text_is_clean_in_full_flow(self):
        def fake_post(url, headers, json, timeout):
            chunk = extract_chunk(json)
            return FakeResponse(
                200,
                ok_payload("```\n" + chunk + "\n```"),
            )

        text = "فقرة أولى.\n\nفقرة ثانية."
        with mock.patch.object(refine.requests, "post", fake_post):
            result = refine.refine_document(text, "conservative")

        self.assertEqual(result["text"], text)


if __name__ == "__main__":
    unittest.main()
