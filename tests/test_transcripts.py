"""اختبارات تطبيع النص العربي وأدوات البحث.

psycopg غير متاح في بعض بيئات التطوير (أندرويد بلا libpq)، فنُبدّله
بوحدة وهمية قبل الاستيراد. الاختبارات هنا لا تلمس قاعدة بيانات.
"""

import sys
import types
import unittest

try:  # pragma: no cover - يعتمد على البيئة
    import psycopg  # noqa: F401
except Exception:  # pragma: no cover
    psycopg = types.ModuleType("psycopg")
    psycopg.connect = lambda *a, **k: None
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object
    psycopg.rows = rows
    sys.modules["psycopg"] = psycopg
    sys.modules["psycopg.rows"] = rows

import transcripts  # noqa: E402


class TestNormalization(unittest.TestCase):
    def test_removes_diacritics_and_tatweel(self):
        self.assertEqual(
            transcripts.normalize_arabic("اَلْمُحَاضَرَة"),
            transcripts.normalize_arabic("المحاضرة"),
        )

    def test_unifies_alef_forms(self):
        normalized = transcripts.normalize_arabic("أصول إسلامية آداب")
        self.assertNotIn("أ", normalized)
        self.assertNotIn("إ", normalized)
        self.assertNotIn("آ", normalized)

    def test_taa_marbuta_becomes_haa(self):
        self.assertEqual(
            transcripts.normalize_arabic("محاضرة"),
            transcripts.normalize_arabic("محاضره"),
        )

    def test_collapses_whitespace(self):
        self.assertEqual(
            transcripts.normalize_arabic("  نص\tعربي\n\nجديد "),
            "نص عربي جديد",
        )

    def test_empty_input(self):
        self.assertEqual(transcripts.normalize_arabic(""), "")
        self.assertEqual(transcripts.normalize_arabic(None), "")


class TestSearchVariants(unittest.TestCase):
    def test_plain_word_has_one_variant(self):
        self.assertEqual(transcripts.search_variants("فقه"), ["فقه"])

    def test_definite_article_is_stripped(self):
        self.assertIn("محاضره", transcripts.search_variants("المحاضرة"))

    def test_short_word_keeps_no_dangerous_prefix(self):
        # «فقه» ليست «قه»؛ إزالة حرف واحد من كلمة قصيرة تخلق نتائج كاذبة.
        self.assertEqual(transcripts.search_variants("فقه"), ["فقه"])

    def test_duplicate_variants_are_removed(self):
        variants = transcripts.search_variants("الاصل")
        self.assertEqual(variants, list(dict.fromkeys(variants)))


class TestBuildSearchTerms(unittest.TestCase):
    def test_one_group_per_word(self):
        terms = transcripts.build_search_terms("أصول الفقه")
        self.assertEqual(len(terms), 2)
        self.assertTrue(all(term for term in terms))

    def test_query_with_only_spaces(self):
        self.assertEqual(transcripts.build_search_terms("   "), [])

    def test_multi_word_query_requires_all_words(self):
        terms = transcripts.build_search_terms("علم أصول")
        self.assertEqual(len(terms), 2)


class _FakeCursor:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row


class _FakeConnection:
    def __init__(self, row):
        self.cursor_obj = _FakeCursor(row)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cursor_obj


class TestCountRawExcluding(unittest.TestCase):
    def setUp(self):
        self.original = transcripts._connect

    def tearDown(self):
        transcripts._connect = self.original

    def test_excludes_requesting_user(self):
        conn = _FakeConnection({"total": 7})
        transcripts._connect = lambda: conn

        self.assertEqual(transcripts.count_raw_excluding(42), 7)
        sql, params = conn.cursor_obj.calls[-1]
        self.assertIn("user_id <> %s", sql)
        self.assertEqual(params, (42,))

    def test_without_exclusion_counts_everyone(self):
        conn = _FakeConnection({"total": 9})
        transcripts._connect = lambda: conn

        self.assertEqual(transcripts.count_raw_excluding(), 9)
        sql, params = conn.cursor_obj.calls[-1]
        self.assertNotIn("user_id <>", sql)
        self.assertIsNone(params)

    def test_failure_returns_none_not_zero(self):
        def boom():
            raise RuntimeError("db down")

        transcripts._connect = boom
        self.assertIsNone(transcripts.count_raw_excluding(42))


if __name__ == "__main__":
    unittest.main()
