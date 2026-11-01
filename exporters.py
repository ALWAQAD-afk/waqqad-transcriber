"""تصدير النصوص إلى TXT وDOCX.

DOCX هنا مبني يدويًا (ZIP + OOXML) دون python-docx/lxml، لسببين:
1. لا نضيف اعتمادية ثقيلة إلى صورة Docker من أجل ملف نصي.
2. يبقى قابلًا للاختبار في كل بيئة، بما فيها البيئات التي لا تُبنى فيها lxml.

الملف الناتج Word-compatible: فقرات عربية بمحاذاة RTL، خط Amiri مع بدائل،
وهوامش A4 قياسية.
"""

import io
import zipfile
from xml.sax.saxutils import escape

DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document"
)
TXT_MIME = "text/plain; charset=utf-8"

DEFAULT_FONT = "Amiri"
DEFAULT_SIZE_PT = 13

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def build_txt(text):
    """TXT بترميز UTF-8 دون BOM، متوافق مع ما يرسله البوت اليوم."""
    return (text or "").encode("utf-8")


def _paragraph(text, font=DEFAULT_FONT, size_pt=DEFAULT_SIZE_PT,
               bold=False, align="right"):
    """فقرة عربية RTL.

    <w:bidi/> يجعل اتجاه الفقرة من اليمين، و<w:rtl/> في خصائص التشكيل
    يضمن أن علامات الترقيم والأرقام تُعرض في موضعها الصحيح.
    """
    half_points = max(2, int(round(size_pt * 2)))

    run_props = (
        "<w:rPr>"
        f'<w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:cs="{font}"/>'
        + ("<w:b/><w:bCs/>" if bold else "")
        + f'<w:sz w:val="{half_points}"/>'
        f'<w:szCs w:val="{half_points}"/>'
        "<w:rtl/>"
        "</w:rPr>"
    )

    para_props = (
        "<w:pPr>"
        "<w:bidi/>"
        f'<w:jc w:val="{align}"/>'
        '<w:spacing w:after="140" w:line="360" w:lineRule="auto"/>'
        "</w:pPr>"
    )

    if not text:
        return f"<w:p>{para_props}</w:p>"

    return (
        "<w:p>"
        + para_props
        + "<w:r>"
        + run_props
        + f'<w:t xml:space="preserve">{escape(text)}</w:t>'
        + "</w:r>"
        + "</w:p>"
    )


def _split_paragraphs(text):
    """سطر فارغ = فقرة جديدة، والسطر الواحد يبقى فقرة مستقلة."""
    paragraphs = []
    buffer = []

    for raw_line in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        if line.strip():
            buffer.append(line.strip())
        elif buffer:
            paragraphs.append(" ".join(buffer))
            buffer = []

    if buffer:
        paragraphs.append(" ".join(buffer))

    return paragraphs


def build_docx(text, title=None):
    """يبني ملف DOCX ويعيده كـ bytes جاهزة للإرسال أو الحفظ."""
    body = []

    if title:
        body.append(_paragraph(title, size_pt=17, bold=True, align="center"))

    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        paragraphs = ["(نص فارغ)"]

    for paragraph in paragraphs:
        body.append(_paragraph(paragraph))

    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}">'
        "<w:body>"
        + "".join(body)
        + "<w:sectPr>"
        "<w:bidi/>"
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" '
        'w:left="1134" w:header="709" w:footer="709" w:gutter="0"/>'
        "</w:sectPr>"
        "</w:body>"
        "</w:document>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("word/document.xml", document)

    return buffer.getvalue()
