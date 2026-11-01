"""حفظ نصوص التفريغ في PostgreSQL.

النص كان يعيش في /tmp فقط، و/tmp على المنصات ذات القرص المؤقت يُمحى عند
كل إعادة تشغيل. هذا الوحدة تحفظ كل نسخة (خام، مصححة، محسّنة، ملخص) في
قاعدة البيانات، فتصبح النسخ والتصدير والبحث ممكنة لاحقًا.

لا تعتمد هذه الوحدة على app.py تجنّبًا للاستيراد الدائري.
"""

import logging
import os

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("transcripts")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

# الأنواع المسموح بها في عمود kind.
KINDS = ("raw", "conservative", "liberal", "summary")


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_table():
    """إنشاء الجدول والفهارس. تُستدعى مرة عند بدء التشغيل."""
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
                    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (job_id, kind)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS transcripts_user_time
                ON transcripts(user_id, created_at DESC)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS transcripts_search
                ON transcripts USING GIN (to_tsvector('simple', content))
            """)
        conn.commit()

    return True


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
                        job_id, user_id, kind, title,
                        language, duration_secs, chars, content
                    )
                    VALUES(%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id, kind) DO UPDATE
                    SET title = EXCLUDED.title,
                        language = EXCLUDED.language,
                        duration_secs = EXCLUDED.duration_secs,
                        chars = EXCLUDED.chars,
                        content = EXCLUDED.content
                """, (
                    str(job_id),
                    int(user_id),
                    kind,
                    title,
                    language,
                    duration_secs,
                    len(content),
                    content,
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


def search_transcripts(user_id, query, limit=8):
    """بحث نصي محصور بصاحب الطلب.

    نجرّب full-text search أولًا (يعمل مع العربية عبر قاموس simple)،
    ثم نعود إلى ILIKE للبحث الجزئي مثل كلمة ناقصة الحروف.
    """
    limit = max(1, min(int(limit), 20))
    query = (query or "").strip()

    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                if not query:
                    cur.execute("""
                        SELECT job_id, kind, title, chars, created_at
                        FROM transcripts
                        WHERE user_id = %s AND kind = 'raw'
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (int(user_id), limit))
                    return cur.fetchall()

                cur.execute("""
                    SELECT job_id, kind, title, chars, created_at
                    FROM transcripts
                    WHERE user_id = %s
                      AND kind = 'raw'
                      AND to_tsvector('simple', content)
                          @@ plainto_tsquery('simple', %s)
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (int(user_id), query, limit))
                rows = cur.fetchall()
                if rows:
                    return rows

                cur.execute("""
                    SELECT job_id, kind, title, chars, created_at
                    FROM transcripts
                    WHERE user_id = %s
                      AND kind = 'raw'
                      AND content ILIKE %s
                    ORDER BY created_at DESC
                    LIMIT %s
                """, (int(user_id), f"%{query}%", limit))
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
