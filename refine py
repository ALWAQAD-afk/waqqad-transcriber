import os
import requests

# لا نقرأ أي ملف عند الاستيراد - فقط متغير البيئة
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-3.5-flash-lite"

def refine_arabic_text(raw_text: str) -> str:
    """
    تصحيح إملائي آمن - لا يكسر السيرفر حتى لو المفتاح غير موجود
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY غير مضبوط في Render Environment")

    # قص النص الطويل
    text = (raw_text or "").strip()[:15000]
    if not text:
        raise RuntimeError("النص فارغ")

    prompt = f"""أنت مدقق لغوي عربي. صحح الأخطاء الإملائية فقط (همزات، تاء مربوطة، علامات ترقيم). لا تلخص ولا تغير المعنى. أعد النص المصحح فقط:

{text}
"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8000}
    }

    r = requests.post(url, json=payload, timeout=90)
    r.raise_for_status()
    data = r.json()

    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        raise RuntimeError(f"Gemini parsing error: {str(data)[:500]}")
