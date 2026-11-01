import io
import unittest
import zipfile
from xml.etree import ElementTree

import exporters

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class TestTxtExport(unittest.TestCase):
    def test_utf8_without_bom(self):
        data = exporters.build_txt("نص عربي")
        self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(data.decode("utf-8"), "نص عربي")

    def test_empty_text(self):
        self.assertEqual(exporters.build_txt(None), b"")


class TestDocxExport(unittest.TestCase):
    def _document_xml(self, payload):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("[Content_Types].xml", archive.namelist())
            self.assertIn("_rels/.rels", archive.namelist())
            return archive.read("word/document.xml")

    def test_package_is_valid_zip_with_required_parts(self):
        payload = exporters.build_docx("محاضرة")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
        self.assertEqual(
            names,
            {"[Content_Types].xml", "_rels/.rels", "word/document.xml"},
        )

    def test_document_xml_is_well_formed(self):
        xml = self._document_xml(exporters.build_docx("مرحبًا"))
        root = ElementTree.fromstring(xml)
        self.assertEqual(root.tag, f"{W_NS}document")

    def test_arabic_text_preserved(self):
        text = "الفقه الإسلامي وأصوله"
        xml = self._document_xml(exporters.build_docx(text))
        self.assertIn(text, xml.decode("utf-8"))

    def test_paragraphs_are_right_to_left(self):
        xml = self._document_xml(exporters.build_docx("سطر أول\n\nسطر ثانٍ"))
        self.assertIn(b"<w:bidi/>", xml)
        self.assertIn(b'<w:jc w:val="right"/>', xml)
        self.assertIn(b"<w:rtl/>", xml)

    def test_blank_line_starts_new_paragraph(self):
        xml = self._document_xml(exporters.build_docx("أ\n\nب"))
        paragraphs = xml.decode("utf-8").count("<w:p>")
        self.assertEqual(paragraphs, 2)

    def test_title_is_added_once(self):
        xml = self._document_xml(
            exporters.build_docx("محتوى", title="عنوان المحاضرة")
        )
        text = xml.decode("utf-8")
        self.assertEqual(text.count("عنوان المحاضرة"), 1)
        self.assertIn('w:val="34"', text)  # 17pt = 34 half-points

    def test_special_characters_are_escaped(self):
        xml = self._document_xml(exporters.build_docx("1 < 2 & 3 > 2"))
        text = xml.decode("utf-8")
        self.assertIn("&lt;", text)
        self.assertIn("&amp;", text)

    def test_empty_text_still_produces_document(self):
        payload = exporters.build_docx("")
        self._document_xml(payload)


if __name__ == "__main__":
    unittest.main()
