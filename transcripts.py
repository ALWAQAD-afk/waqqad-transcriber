"""حفظ نصوص التفريغ في PostgreSQL والبحث فيها.

النص كان يعيش في /tmp فقط، و/tmp على المنصات ذات القرص المؤقت يُمحى عند
كل إعادة تشغيل. هذه الوحدة تحفظ كل نسخة (خام، مصححة، محسّنة، ملخص) في
قاعدة البيانات، فتصبح النسخ والتصدير والبحث ممكنة لاحقًا.

البحث يعمل على نص التفريغ كاملًا (لا على اسم الملف)، بعد تطبيع عربي
يجعل «المحاضرة» تجد «محاضرة»، و«أصول» تجد «اصول»، ويتجاهل التشكيل
والتطويل.

لا تعتمد هذه الوحدة على app.py تجنّبًا للاستيراد الدائري.
"""

import logging
import os
import re
import unicodedata

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("transcripts")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

KINDS = ("raw", "conservative", "liberal", "summary")

# التشكيل وعلامات الضبط والتطويل: لا قيمة لها في البحث.
_DIACRITICS_RE = re.compile(
    "[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED\u0640]"
)

_NORMALIZE_MAP = str.maketrans({
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ٱ": "ا",
    "ى": "ي",
    "ؤ": "و",
    "ئ": "ي",
    "ة": "ه",
})

# السوابق العربية الشائعة التي تُلتصق بالكلمة فتخفيها عن البحث.
_PREFIXES = ("وال", "بال", "كال", "فال", "ال", "لل", "و", "ب", "ل", "ك", "ف")


def normalize_arabic(text):
    """تطبيع نص عربي للبحث: بلا تشكيل، بهمزات موحّدة، بحروف صغيرة."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = _DIACRITICS_RE.sub("", text)
    text = text.translate(_NORMALIZE_MAP)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def search_variants(token):
    """الكلمة نفسها، ثم الكلمة بعد إزالة سابقة عربية إن وُجدت."""
    normalized = normalize_arabic(token)
    if not normalized:
        return []

    variants = [normalized]

    for prefix in _PREFIXES:
        if normalized.startswith(prefix) and len(normalized) - len(prefix) >= 3:
            variants.append(normalized[len(prefix):])

    # إبقاء الترتيب وحذف التكرار.
    return list(dict.fromkeys(variants))


def build_search_terms(query):
    """يعيد قائمة الشروط: لكل كلمة في الاستعلام بدائلها."""
    tokens = [t for t in normalize_arabic(query).split(" ") if t]
    return [search_variants(token) for token in tokens]


from db import get_db_connection


def _connect():
    return get_db_connection()


def init_table():
    """إنشاء الجدول والفهارس، وإضافة عمود البحث لأول مرة."""
    if not DATABASE_URL:
        log.warning("DATABASE_URL is not set; transcripts storage disabled")
        return False

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS transcripts (
                    id            BIGSERIAL PRIMARY KEY,
                    job_id        TEXT NOT NULL,
                    user_id       BIGINT NOT NULL,
                    kind          TEXT NOT NULL,
                    title         TEXT,
                    language      TEXT,
                    duration_secs INTEGER,
                    chars         INTEGER NOT NULL DEFAULT 0,
                    content       TEXT NOT NULL,
                    content_norm  TEXT,
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (job_id, kind)
                )
            """)
            cur.execute("""
                ALTER TABLE transcripts
                ADD COLUMN IF NOT EXISTS content_norm TEXT
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS transcripts_user_time
                ON transcripts(user_id, created_at DESC)
            """)
        conn.commit()

        # فهرس trigram يسرّع البحث الجزئي؛ إن لم تسمح القاعدة بالامتداد
        # يبقى البحث صحيحًا لكن أبطأ.
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS transcripts_norm_trgm
                    ON transcripts USING GIN (content_norm gin_trgm_ops)
                """)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            log.info("pg_trgm unavailable, skipping trigram index: %s", exc)

    _backfill_normalized()
    return True


def _backfill_normalized(limit=500):
    """تعبئة content_norm للصفوف القديمة."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, content FROM transcripts
                    WHERE content_norm IS NULL
                    ORDER BY id
                    LIMIT %s
                """, (limit,))
                rows = cur.fetchall()

                for row in rows:
                    cur.execute(
                        "UPDATE transcripts SET content_norm = %s WHERE id = %s",
                        (normalize_arabic(row["content"]), row["id"]),
                    )
            conn.commit()

        if rows:
            log.info("Backfilled %s transcript search rows", len(rows))
    except Exception as exc:
        log.warning("Could not backfill search column: %s", exc)


def save_transcript(
    job_id,
    user_id,
    kind,
    content,
    title=None,
    language=None,
    duration_secs=None,
):
    """حفظ/تحديث نسخة واحدة. يعيد False إن تعذّر الحفظ دون رفع استثناء."""
    if not job_id or not user_id or not content:
        return False
    if kind not in KINDS:
        raise ValueError(f"Unknown transcript kind: {kind}")

    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO transcripts(
                        job_id, user_id, kind, title, language,
                        duration_secs, chars, content, content_norm
                    )
                    VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id, kind) DO UPDATE
                    SET title = EXCLUDED.title,
                        language = EXCLUDED.language,
                        duration_secs = EXCLUDED.duration_secs,
                        chars = EXCLUDED.chars,
                        content = EXCLUDED.content,
                        content_norm = EXCLUDED.content_norm
                """, (
                    str(job_id),
                    int(user_id),
                    kind,
                    title,
                    language,
                    duration_secs,
                    len(content),
                    content,
                    normalize_arabic(content),
                ))
            conn.commit()
        return True
    except Exception as exc:
        log.warning("Could not save transcript %s/%s: %s", job_id, kind, exc)
        return False


def get_transcript(job_id, kind):
    """نسخة واحدة أو None."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT job_id, user_id, kind, title, chars, content, created_at
                    FROM transcripts
                    WHERE job_id = %s AND kind = %s
                """, (str(job_id), kind))
                return cur.fetchone()
    except Exception as exc:
        log.warning("Could not read transcript %s/%s: %s", job_id, kind, exc)
        return None


def available_kinds(job_id):
    """الأنواع المحفوظة لمهمة معينة، مفيدة لبناء أزرار التصدير."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT kind FROM transcripts WHERE job_id = %s
                """, (str(job_id),))
                return {row["kind"] for row in cur.fetchall()}
    except Exception as exc:
        log.warning("Could not list transcripts for %s: %s", job_id, exc)
        return set()


def _recent(user_id, limit):
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT job_id, kind, title, chars, created_at
                FROM transcripts
                WHERE user_id = %s AND kind = 'raw'
                ORDER BY created_at DESC
                LIMIT %s
            """, (int(user_id), limit))
            return cur.fetchall()


def search_transcripts(user_id, query, limit=8):
    """بحث نصي محصور بصاحب الطلب.

    كل كلمة في الاستعلام يجب أن تظهر (بأحد بدائلها) داخل النص، بلا حساسية
    للتشكيل ولا للسوابق ولا للهمزات.
    """
    limit = max(1, min(int(limit), 20))
    query = (query or "").strip()

    try:
        if not query:
            return _recent(user_id, limit)

        terms = build_search_terms(query)
        if not terms:
            return _recent(user_id, limit)

        conditions = []
        params = [int(user_id)]

        for variants in terms:
            placeholders = " OR ".join(["content_norm LIKE %s"] * len(variants))
            conditions.append(f"({placeholders})")
            params.extend(f"%{variant}%" for variant in variants)

        params.append(limit)

        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"""
                    SELECT job_id, kind, title, chars, created_at
                    FROM transcripts
                    WHERE user_id = %s
                      AND kind = 'raw'
                      AND {" AND ".join(conditions)}
                    ORDER BY created_at DESC
                    LIMIT %s
                """, params)
                return cur.fetchall()
    except Exception as exc:
        log.warning("Search failed for user %s: %s", user_id, exc)
        return []


def user_stats(user_id):
    """أرقام بسيطة لصفحة إحصاءات المستخدم (تُستعمل في مرحلة لاحقة)."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS total,
                           COALESCE(SUM(chars), 0) AS chars,
                           MAX(created_at) AS last_at
                    FROM transcripts
                    WHERE user_id = %s AND kind = 'raw'
                """, (int(user_id),))
                return cur.fetchone()
    except Exception as exc:
        log.warning("Stats failed for user %s: %s", user_id, exc)
        return {"total": 0, "chars": 0, "last_at": None}


def overall_stats():
    """عدّاد خفيف لصفحة التشخيص."""
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) FILTER (WHERE kind = 'raw') AS transcripts,
                           COUNT(*) AS rows,
                           COUNT(DISTINCT user_id) AS users,
                           COUNT(*) FILTER (
                               WHERE created_at > NOW() - INTERVAL '24 hours'
                           ) AS last_day
                    FROM transcripts
                """)
                row = cur.fetchone()
                return row or {}
    except Exception as exc:
        log.warning("Overall stats failed: %s", exc)
        return {}
