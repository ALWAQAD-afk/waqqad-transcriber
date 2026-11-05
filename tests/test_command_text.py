import unittest

from command_text import normalize_command_text


class TestNormalizeCommandText(unittest.TestCase):
    def test_normal_slash_is_untouched(self):
        text = "/ytinfo https://youtu.be/abc"
        self.assertEqual(normalize_command_text(text), text)

    def test_arabic_decimal_separator_lookalike(self):
        self.assertEqual(
            normalize_command_text("٫ytinfo https://youtu.be/abc"),
            "/ytinfo https://youtu.be/abc"
        )

    def test_backslash_lookalike(self):
        self.assertEqual(
            normalize_command_text("\\allusers"),
            "/allusers"
        )

    def test_full_width_slash(self):
        self.assertEqual(
            normalize_command_text("／ytinfo https://youtu.be/abc"),
            "/ytinfo https://youtu.be/abc"
        )

    def test_developer_bare_command(self):
        self.assertEqual(
            normalize_command_text(
                "ytinfo https://youtu.be/abc",
                developer=True,
            ),
            "/ytinfo https://youtu.be/abc"
        )

    def test_bare_command_for_non_developer_is_untouched(self):
        text = "ytinfo https://youtu.be/abc"
        self.assertEqual(normalize_command_text(text), text)

    def test_unknown_word_after_lookalike_is_untouched(self):
        text = "٫مرحبا بالعالم"
        self.assertEqual(normalize_command_text(text), text)

    def test_plain_text_is_untouched(self):
        text = "https://youtu.be/abc"
        self.assertEqual(normalize_command_text(text), text)

    def test_empty_text(self):
        self.assertEqual(normalize_command_text(""), "")
        self.assertIsNone(normalize_command_text(None))


if __name__ == "__main__":
    unittest.main()
