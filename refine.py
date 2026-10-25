
import os
import time
import requests
import re

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = "gemini-3.5-flash-lite"

# --- تقسيم ذكي يحترم نهاية الجملة ---
def split_text_smart(text, max_chars=7000):
    """يقسم النص لدفعات مع احترام الفقرات والجمل"""
    if len(text) <= max_chars:
        return [text]
    
    chunks = []
    # نقسم بالفقرات أولاً
    paragraphs = re.split(r'\n\s*\n', text)
    
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_chars:
            current += ("\n\n" + para if current else para)
        else:
            if current:
                chunks.append(current)
                current = ""
            
            # الفقرة نفسها طويلة جداً -> نقسمها بالجمل
            if len(para) > max_chars:
                sentences = re.split(r'(?<=[.!؟،]\s+)', para)
                temp = ""
                for sent in sentences:
                    if len(temp) + len(sent) <= max_chars:
                        temp += sent
                    else:
                        if temp:
                            chunks.append(temp)
                        temp = sent
                if temp:
                    current = temp
            else:
                current = para
    
    if current:
        chunks.append(current)
    
    return chunks

def call_gemini(prompt, max_tokens=8000, retries=2):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": max_tokens}
    }
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=120)
            r.raise_for_status()
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(2)

# --- المستوى 1: محافظ جداً ---
def refine_chunk_conservative(chunk, idx, total):
    prompt = f"""أنت مدقق إملائي محافظ جداً (دفعة {idx}/{total}).
قواعد صارمة جداً:
1. ممنوع منعاً باتاً تغيير أي كلمة أو حرف أو إضافة أو حذف.
2. فقط صحح: الهمزات (أ إ آ ء)، التاء المربوطة (ة بدل ه في آخر الكلمة)، الياء المقصورة (ى).
3. أضف علامة ترقيم بسيطة (، .) عند الحاجة فقط.
4. اترك الشعر والمصطلحات القديمة كما هي تماماً. كلمة "أتظعن" تبقى "أتظعن" لا تضف تاء.
5. أعد النص نفسه بعد التصحيح الإملائي فقط.

النص:
---
{chunk}
---
النص المصحح:
"""
    return call_gemini(prompt, max_tokens=8000)

# --- المستوى 2: حر ---
def refine_chunk_liberal(chunk, idx, total):
    prompt = f"""أنت محرر عربي محترف (دفعة {idx}/{total}).
مهمتك تحسين القراءة مع الحفاظ على المعنى:
1. صحح الإملاء والهمزات والتاء المربوطة.
2. رتب الفقرات، ضع عناوين فرعية إذا كان النص محاضرة، أضف ترقيم واضح.
3. يمكنك تحسين الصياغة قليلاً لتكون أوضح مع الحفاظ على المعنى الأصلي تماماً.
4. لا تحذف أي فكرة مهمة.

النص:
---
{chunk}
---
النص المحسن:
"""
    return call_gemini(prompt, max_tokens=8000)

def refine_in_batches(raw_text, mode="conservative"):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY غير موجود")
    
    raw_text = raw_text.strip()
    if not raw_text:
        return ""
    
    chunks = split_text_smart(raw_text, max_chars=7000)
    print(f"[REFINE {mode}] {len(raw_text)} chars -> {len(chunks)} batches", flush=True)
    
    refined_parts = []
    for i, chunk in enumerate(chunks, 1):
        print(f"[REFINE {mode}] Batch {i}/{len(chunks)} ({len(chunk)} chars)", flush=True)
        try:
            if mode == "conservative":
                refined = refine_chunk_conservative(chunk, i, len(chunks))
            else:
                refined = refine_chunk_liberal(chunk, i, len(chunks))
            refined_parts.append(refined)
            # احترام حدود Gemini
            time.sleep(1)
        except Exception as e:
            print(f"[REFINE {mode}] Batch {i} failed: {e}, using original", flush=True)
            refined_parts.append(chunk)  # في حال الفشل نعيد الأصل
    
    return "\n\n".join(refined_parts)

def refine_conservative(text):
    return refine_in_batches(text, mode="conservative")

def refine_liberal(text):
    return refine_in_batches(text, mode="liberal")

# واجهة قديمة للتوافق
def refine_arabic_text(text):
    return refine_conservative(text)
