import os
import json
import html
import logging
from datetime import datetime, timezone

import requests
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


# ============================================================
# Alwarraq RC3.2 — Mini App polish + cache support
# Fast Telegram file library
# ============================================================

BOT_TOKEN = os.getenv("WARRAQ_BOT_TOKEN", "").strip()
STORAGE_CHAT_ID = os.getenv("WARRAQ_STORAGE_CHAT_ID", "").strip()
DATABASE_URL = os.getenv("WARRAQ_DATABASE_URL", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("WARRAQ_BOT_TOKEN is not configured")
if not STORAGE_CHAT_ID:
    raise RuntimeError("WARRAQ_STORAGE_CHAT_ID is not configured")
if not DATABASE_URL:
    raise RuntimeError("WARRAQ_DATABASE_URL is not configured")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PAGE_SIZE = 8
MINIAPP_URL = os.getenv("WARRAQ_MINIAPP_URL", "https://waqqad-transcriber.onrender.com/warraq").strip()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("alwarraq")

# Reuse HTTPS/TLS connections to Telegram instead of reconnecting every request.
HTTP = requests.Session()

# Keep PostgreSQL connections warm instead of opening a new TLS connection
# on every button press.
POOL = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=4,
    timeout=10,
    kwargs={
        "row_factory": dict_row,
        "autocommit": False,
    },
    open=True,
)

DEFAULT_FOLDERS = [
    ("كتب شرعية", "📚"),
    ("كتب أدبية", "📖"),
    ("كتب لغوية", "📝"),
    ("كتب تاريخية", "🏺"),
    ("الجامعة", "🎓"),
    ("المحاضرات", "🎧"),
    ("المشاريع", "🛠️"),
    ("أخرى", "📦"),
]


# ============================================================
# Database
# ============================================================

def run_tx(fn):
    """Run a small transaction using a warm pooled connection."""
    with POOL.connection() as conn:
        try:
            result = fn(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise


def init_db():
    def _work(conn):
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '📁',
                parent_id BIGINT REFERENCES folders(id) ON DELETE RESTRICT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS folders_unique_root
            ON folders(user_id, lower(name))
            WHERE parent_id IS NULL
        """)

        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS folders_unique_child
            ON folders(user_id, parent_id, lower(name))
            WHERE parent_id IS NOT NULL
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                telegram_file_id TEXT,
                file_unique_id TEXT,
                file_name TEXT NOT NULL DEFAULT 'Telegram file',
                file_size BIGINT NOT NULL DEFAULT 0,
                mime_type TEXT,
                media_type TEXT,
                source_chat_id BIGINT,
                source_message_id BIGINT,
                storage_message_id BIGINT,
                folder_id BIGINT REFERENCES folders(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                saved_at TIMESTAMPTZ
            )
        """)

        # Safe migration from the previous schema.
        migrations = [
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS telegram_file_id TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS file_unique_id TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS file_name TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS file_size BIGINT DEFAULT 0",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS mime_type TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS media_type TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS source_chat_id BIGINT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS source_message_id BIGINT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS storage_message_id BIGINT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS folder_id BIGINT REFERENCES folders(id) ON DELETE SET NULL",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'saved'",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS saved_at TIMESTAMPTZ",
        ]
        for sql in migrations:
            cur.execute(sql)

        # If an older schema used filename, preserve it.
        cur.execute("""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name='files' AND column_name='filename'
        """)
        if cur.fetchone():
            cur.execute("""
                UPDATE files
                SET file_name = COALESCE(NULLIF(file_name, ''), filename, 'Telegram file')
                WHERE file_name IS NULL OR file_name='' OR file_name='Telegram file'
            """)

        cur.execute("""
            UPDATE files
            SET file_name='Telegram file'
            WHERE file_name IS NULL OR file_name=''
        """)

        cur.execute("""
            UPDATE files SET status='saved'
            WHERE status IS NULL
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS files_user_folder_idx
            ON files(user_id, folder_id, id DESC)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS files_user_name_idx
            ON files(user_id, lower(file_name))
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS files_unique_idx
            ON files(user_id, file_unique_id)
        """)

        cur.execute("""
            CREATE INDEX IF NOT EXISTS files_pending_idx
            ON files(user_id, status, id)
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id BIGINT PRIMARY KEY,
                action TEXT,
                target_id BIGINT,
                data JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

    run_tx(_work)


_db_ready = False


def ensure_db():
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True


# ============================================================
# Telegram API
# ============================================================

def tg(method, payload=None, timeout=30):
    r = HTTP.post(
        f"{API}/{method}",
        json=payload or {},
        timeout=timeout,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data}")
    return data["result"]


def send(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg("sendMessage", payload)


def edit(chat_id, message_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    try:
        return tg("editMessageText", payload)
    except Exception as e:
        if "message is not modified" not in str(e):
            raise


def answer_callback(callback_id, text=None, alert=False):
    payload = {
        "callback_query_id": callback_id,
        "show_alert": alert,
    }
    if text:
        payload["text"] = text
    try:
        tg("answerCallbackQuery", payload)
    except Exception:
        log.exception("answerCallbackQuery failed")


def copy_message(chat_id, from_chat_id, message_id, caption=None):
    payload = {
        "chat_id": chat_id,
        "from_chat_id": from_chat_id,
        "message_id": message_id,
    }
    if caption is not None:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    return tg("copyMessage", payload, timeout=60)


def delete_message(chat_id, message_id):
    try:
        tg("deleteMessage", {
            "chat_id": chat_id,
            "message_id": message_id,
        })
    except Exception:
        log.exception("deleteMessage failed")


def btn(text, data):
    return {"text": text, "callback_data": data}


def webapp_btn(text, url=None):
    return {
        "text": text,
        "web_app": {"url": url or MINIAPP_URL},
    }


def esc(value):
    return html.escape(str(value or ""))


def human_size(n):
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.2f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


# ============================================================
# State
# ============================================================

def set_state(user_id, action=None, target_id=None, data=None):
    def _work(conn):
        conn.execute("""
            INSERT INTO user_states(user_id, action, target_id, data, updated_at)
            VALUES(%s,%s,%s,%s::jsonb,NOW())
            ON CONFLICT(user_id) DO UPDATE SET
                action=EXCLUDED.action,
                target_id=EXCLUDED.target_id,
                data=EXCLUDED.data,
                updated_at=NOW()
        """, (
            user_id,
            action,
            target_id,
            json.dumps(data or {}),
        ))
    run_tx(_work)


def clear_state(user_id):
    set_state(user_id, None, None, {})


def get_state(user_id):
    def _work(conn):
        return conn.execute(
            "SELECT action,target_id,data FROM user_states WHERE user_id=%s",
            (user_id,),
        ).fetchone()
    return run_tx(_work)


# ============================================================
# User and folders
# ============================================================

def ensure_user(user_id):
    """
    One DB round trip. Inserts defaults only if the user has no folders yet.
    """
    def _work(conn):
        cur = conn.execute(
            "SELECT 1 FROM folders WHERE user_id=%s LIMIT 1",
            (user_id,),
        )
        if cur.fetchone():
            return

        for name, icon in DEFAULT_FOLDERS:
            conn.execute("""
                INSERT INTO folders(user_id,name,icon,parent_id)
                VALUES(%s,%s,%s,NULL)
                ON CONFLICT DO NOTHING
            """, (user_id, name, icon))

    run_tx(_work)


def folder_info(user_id, folder_id):
    if folder_id is None:
        return None

    def _work(conn):
        return conn.execute("""
            SELECT id,name,icon,parent_id
            FROM folders
            WHERE id=%s AND user_id=%s
        """, (folder_id, user_id)).fetchone()

    return run_tx(_work)


def folder_path(user_id, folder_id):
    """
    One recursive SQL query instead of one query per parent folder.
    """
    if folder_id is None:
        return "ملفاتي"

    def _work(conn):
        rows = conn.execute("""
            WITH RECURSIVE tree AS (
                SELECT id,name,parent_id,1 AS depth
                FROM folders
                WHERE id=%s AND user_id=%s

                UNION ALL

                SELECT f.id,f.name,f.parent_id,t.depth+1
                FROM folders f
                JOIN tree t ON f.id=t.parent_id
                WHERE f.user_id=%s
            )
            SELECT name
            FROM tree
            ORDER BY depth DESC
        """, (folder_id, user_id, user_id)).fetchall()

        return " / ".join(row["name"] for row in rows) or "ملفاتي"

    return run_tx(_work)


def folder_snapshot(user_id, folder_id, page):
    """
    One pooled connection for the whole folder screen.
    Returns folder name, parent, children, files, and next-page flag.
    """
    offset = max(0, page) * PAGE_SIZE

    def _work(conn):
        if folder_id is None:
            folder = None
            children = conn.execute("""
                SELECT id,name,icon,parent_id
                FROM folders
                WHERE user_id=%s AND parent_id IS NULL
                ORDER BY id
            """, (user_id,)).fetchall()

            file_rows = conn.execute("""
                SELECT id,file_name,file_size,folder_id
                FROM files
                WHERE user_id=%s
                  AND folder_id IS NULL
                  AND status='saved'
                ORDER BY id DESC
                LIMIT %s OFFSET %s
            """, (user_id, PAGE_SIZE + 1, offset)).fetchall()

        else:
            folder = conn.execute("""
                SELECT id,name,icon,parent_id
                FROM folders
                WHERE id=%s AND user_id=%s
            """, (folder_id, user_id)).fetchone()

            if not folder:
                return None

            children = conn.execute("""
                SELECT id,name,icon,parent_id
                FROM folders
                WHERE user_id=%s AND parent_id=%s
                ORDER BY lower(name),id
            """, (user_id, folder_id)).fetchall()

            file_rows = conn.execute("""
                SELECT id,file_name,file_size,folder_id
                FROM files
                WHERE user_id=%s
                  AND folder_id=%s
                  AND status='saved'
                ORDER BY id DESC
                LIMIT %s OFFSET %s
            """, (user_id, folder_id, PAGE_SIZE + 1, offset)).fetchall()

        has_next = len(file_rows) > PAGE_SIZE
        return {
            "folder": folder,
            "children": children,
            "files": file_rows[:PAGE_SIZE],
            "has_next": has_next,
        }

    return run_tx(_work)


def create_folder(user_id, parent_id, name):
    name = " ".join((name or "").strip().split())
    if not name:
        raise ValueError("اسم المجلد فارغ.")
    if len(name) > 60:
        raise ValueError("اسم المجلد طويل جدًا.")
    if "/" in name:
        raise ValueError("لا تستخدم / داخل اسم المجلد.")

    def _work(conn):
        if parent_id is not None:
            parent = conn.execute(
                "SELECT 1 FROM folders WHERE id=%s AND user_id=%s",
                (parent_id, user_id),
            ).fetchone()
            if not parent:
                raise ValueError("المجلد الأب غير موجود.")

        if parent_id is None:
            exists = conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s
                  AND parent_id IS NULL
                  AND lower(name)=lower(%s)
            """, (user_id, name)).fetchone()
        else:
            exists = conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s
                  AND parent_id=%s
                  AND lower(name)=lower(%s)
            """, (user_id, parent_id, name)).fetchone()

        if exists:
            raise ValueError("يوجد مجلد بهذا الاسم هنا بالفعل.")

        return conn.execute("""
            INSERT INTO folders(user_id,name,icon,parent_id)
            VALUES(%s,%s,'📁',%s)
            RETURNING id
        """, (user_id, name, parent_id)).fetchone()["id"]

    return run_tx(_work)


def rename_folder(user_id, folder_id, name):
    name = " ".join((name or "").strip().split())
    if not name or len(name) > 60 or "/" in name:
        raise ValueError("اسم المجلد غير صالح.")

    def _work(conn):
        folder = conn.execute("""
            SELECT id,parent_id
            FROM folders
            WHERE id=%s AND user_id=%s
        """, (folder_id, user_id)).fetchone()

        if not folder:
            raise ValueError("المجلد غير موجود.")

        if folder["parent_id"] is None:
            exists = conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s
                  AND parent_id IS NULL
                  AND lower(name)=lower(%s)
                  AND id<>%s
            """, (user_id, name, folder_id)).fetchone()
        else:
            exists = conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s
                  AND parent_id=%s
                  AND lower(name)=lower(%s)
                  AND id<>%s
            """, (
                user_id,
                folder["parent_id"],
                name,
                folder_id,
            )).fetchone()

        if exists:
            raise ValueError("يوجد مجلد بهذا الاسم هنا بالفعل.")

        conn.execute("""
            UPDATE folders
            SET name=%s
            WHERE id=%s AND user_id=%s
        """, (name, folder_id, user_id))

    run_tx(_work)


def remove_empty_folder(user_id, folder_id):
    def _work(conn):
        folder = conn.execute("""
            SELECT id,parent_id
            FROM folders
            WHERE id=%s AND user_id=%s
        """, (folder_id, user_id)).fetchone()

        if not folder:
            raise ValueError("المجلد غير موجود.")

        child = conn.execute("""
            SELECT 1 FROM folders
            WHERE user_id=%s AND parent_id=%s
            LIMIT 1
        """, (user_id, folder_id)).fetchone()

        file_row = conn.execute("""
            SELECT 1 FROM files
            WHERE user_id=%s
              AND folder_id=%s
              AND status='saved'
            LIMIT 1
        """, (user_id, folder_id)).fetchone()

        if child or file_row:
            raise ValueError("المجلد غير فارغ. انقل/احذف محتواه أولًا.")

        conn.execute("""
            DELETE FROM folders
            WHERE id=%s AND user_id=%s
        """, (folder_id, user_id))

        return folder["parent_id"]

    return run_tx(_work)


# ============================================================
# Files
# ============================================================

def extract_file(message):
    candidates = [
        ("document", message.get("document")),
        ("audio", message.get("audio")),
        ("video", message.get("video")),
        ("voice", message.get("voice")),
        ("animation", message.get("animation")),
    ]

    for kind, obj in candidates:
        if obj:
            default_name = {
                "document": "Telegram file",
                "audio": "Audio",
                "video": "Video",
                "voice": "Voice message",
                "animation": "Animation",
            }[kind]

            return {
                "telegram_file_id": obj.get("file_id"),
                "file_unique_id": obj.get("file_unique_id"),
                "file_name": obj.get("file_name") or default_name,
                "file_size": obj.get("file_size") or 0,
                "mime_type": obj.get("mime_type") or (
                    "application/octet-stream" if kind == "document" else None
                ),
                "media_type": kind,
            }

    photos = message.get("photo")
    if photos:
        obj = photos[-1]
        return {
            "telegram_file_id": obj.get("file_id"),
            "file_unique_id": obj.get("file_unique_id"),
            "file_name": "Photo.jpg",
            "file_size": obj.get("file_size") or 0,
            "mime_type": "image/jpeg",
            "media_type": "photo",
        }

    return None


def pending_snapshot(user_id):
    def _work(conn):
        return conn.execute("""
            SELECT id,file_name,file_size
            FROM files
            WHERE user_id=%s AND status='pending'
            ORDER BY id
        """, (user_id,)).fetchall()
    return run_tx(_work)


def pending_count(user_id):
    def _work(conn):
        return conn.execute("""
            SELECT COUNT(*) AS n
            FROM files
            WHERE user_id=%s AND status='pending'
        """, (user_id,)).fetchone()["n"]
    return run_tx(_work)


def find_duplicate(user_id, unique_id):
    if not unique_id:
        return None

    def _work(conn):
        return conn.execute("""
            SELECT id,file_name,file_size,folder_id
            FROM files
            WHERE user_id=%s
              AND file_unique_id=%s
              AND status='saved'
            ORDER BY id DESC
            LIMIT 1
        """, (user_id, unique_id)).fetchone()

    return run_tx(_work)


def insert_pending(user_id, f):
    def _work(conn):
        return conn.execute("""
            INSERT INTO files(
                user_id,
                telegram_file_id,
                file_unique_id,
                file_name,
                file_size,
                mime_type,
                media_type,
                source_chat_id,
                source_message_id,
                status
            )
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
            RETURNING id
        """, (
            user_id,
            f["telegram_file_id"],
            f["file_unique_id"],
            f["file_name"],
            f["file_size"],
            f["mime_type"],
            f.get("media_type"),
            f.get("source_chat_id"),
            f.get("source_message_id"),
        )).fetchone()["id"]

    return run_tx(_work)


def get_file(user_id, file_id, saved_only=False):
    def _work(conn):
        sql = """
            SELECT id,user_id,telegram_file_id,file_unique_id,file_name,
                   file_size,mime_type,media_type,source_chat_id,source_message_id,
                   storage_message_id,folder_id,status
            FROM files
            WHERE id=%s AND user_id=%s
        """
        params = [file_id, user_id]
        if saved_only:
            sql += " AND status='saved'"
        return conn.execute(sql, params).fetchone()

    return run_tx(_work)


def search_files(user_id, query, limit=20):
    query = query.strip()
    if not query:
        return []

    def _work(conn):
        return conn.execute("""
            SELECT id,file_name,file_size,folder_id
            FROM files
            WHERE user_id=%s
              AND status='saved'
              AND file_name ILIKE %s
            ORDER BY id DESC
            LIMIT %s
        """, (user_id, f"%{query}%", limit)).fetchall()

    return run_tx(_work)


def claim_pending(user_id, file_id):
    def _work(conn):
        return conn.execute("""
            UPDATE files
            SET status='saving'
            WHERE id=%s
              AND user_id=%s
              AND status='pending'
            RETURNING id,telegram_file_id,file_name,file_size,mime_type,media_type,
                      source_chat_id,source_message_id
        """, (file_id, user_id)).fetchone()

    return run_tx(_work)


def unclaim_pending(user_id, file_id):
    def _work(conn):
        conn.execute("""
            UPDATE files
            SET status='pending'
            WHERE id=%s
              AND user_id=%s
              AND status='saving'
        """, (file_id, user_id))
    run_tx(_work)


def finish_saved(user_id, file_id, storage_message_id, folder_id):
    def _work(conn):
        conn.execute("""
            UPDATE files
            SET storage_message_id=%s,
                folder_id=%s,
                status='saved',
                saved_at=NOW()
            WHERE id=%s
              AND user_id=%s
              AND status='saving'
        """, (
            storage_message_id,
            folder_id,
            file_id,
            user_id,
        ))
    run_tx(_work)


def save_one(user_id, pending_id, folder_id):
    if folder_id is not None:
        info = folder_info(user_id, folder_id)
        if not info:
            raise ValueError("المجلد غير موجود.")

    row = claim_pending(user_id, pending_id)
    if not row:
        return False

    caption = (
        "📚 <b>الوَرّاق</b>\n\n"
        f"📄 {esc(row['file_name'])}\n"
        f"📦 {human_size(row['file_size'])}\n"
        f"👤 المالك: <code>{user_id}</code>\n"
        f"#warraq #u_{user_id}"
    )

    storage_message_id = None

    try:
        if row.get("source_chat_id") and row.get("source_message_id"):
            result = copy_message(
                STORAGE_CHAT_ID,
                row["source_chat_id"],
                row["source_message_id"],
                caption=caption,
            )
            storage_message_id = result["message_id"]
        else:
            media_type = row.get("media_type")
            mime = (row.get("mime_type") or "").lower()

            if not media_type:
                if mime.startswith("image/"):
                    media_type = "photo"
                elif mime.startswith("video/"):
                    media_type = "video"
                elif mime.startswith("audio/"):
                    media_type = "audio"
                else:
                    media_type = "document"

            method_and_field = {
                "photo": ("sendPhoto", "photo"),
                "video": ("sendVideo", "video"),
                "audio": ("sendAudio", "audio"),
                "voice": ("sendVoice", "voice"),
                "animation": ("sendAnimation", "animation"),
                "document": ("sendDocument", "document"),
            }

            method, field = method_and_field.get(media_type, ("sendDocument", "document"))

            result = tg(
                method,
                {
                    "chat_id": STORAGE_CHAT_ID,
                    field: row["telegram_file_id"],
                    "caption": caption,
                    "parse_mode": "HTML",
                },
                timeout=60,
            )
            storage_message_id = result["message_id"]

        finish_saved(user_id, pending_id, storage_message_id, folder_id)
        return True

    except Exception:
        unclaim_pending(user_id, pending_id)
        if storage_message_id:
            try:
                delete_message(STORAGE_CHAT_ID, storage_message_id)
            except Exception:
                pass
        raise


def save_many(user_id, file_ids, folder_id):
    saved = []
    for file_id in file_ids:
        if save_one(user_id, file_id, folder_id):
            saved.append(file_id)
    return saved


def delete_saved_file(user_id, file_id):
    row = get_file(user_id, file_id, saved_only=True)
    if not row:
        return False, None

    if row["storage_message_id"]:
        delete_message(
            STORAGE_CHAT_ID,
            row["storage_message_id"],
        )

    def _work(conn):
        conn.execute("""
            DELETE FROM files
            WHERE id=%s AND user_id=%s
        """, (file_id, user_id))

    run_tx(_work)
    return True, row["folder_id"]


# ============================================================
# Screens
# ============================================================

def home_screen(chat_id, user_id, message_id=None):
    ensure_user(user_id)
    count = pending_count(user_id)

    text = (
        "📚 <b>الوَرّاق</b>\n\n"
        "أرسل ملفاتك هنا مباشرة، ثم نظّمها من الواجهة.\n"
        "أو افتح مكتبتك الآن:"
    )

    if count:
        text += f"\n\n📥 لديك <b>{count}</b> ملف/ملفات بانتظار التنظيم."

    keyboard = [
        [webapp_btn("📚 فتح الوَرّاق")],
        [
            btn("📂 ملفاتي", "browse:root:0"),
            btn("🔎 بحث", "search"),
        ],
    ]

    if count:
        keyboard.append([
            btn(f"📚 تنظيم المعلّق ({count})", "pending")
        ])

    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, keyboard)


def browse_screen(
    chat_id,
    user_id,
    folder_id=None,
    page=0,
    message_id=None,
    selecting=False,
):
    snap = folder_snapshot(user_id, folder_id, page)

    if snap is None:
        text = "❌ المجلد غير موجود."
        keyboard = [[btn("🏠 الرئيسية", "home")]]
        edit(chat_id, message_id, text, keyboard)
        return

    path = folder_path(user_id, folder_id)

    text = f"📂 <b>{esc(path)}</b>"

    if selecting:
        text += "\n\nاختر هذا المجلد للحفظ، أو افتح مجلدًا داخله."
    elif not snap["children"] and not snap["files"]:
        text += "\n\nهذا المجلد فارغ."

    keyboard = []

    for folder in snap["children"]:
        prefix = "pick" if selecting else "browse"
        keyboard.append([
            btn(
                f"{folder['icon']} {folder['name']}",
                f"{prefix}:{folder['id']}:0",
            )
        ])

    if not selecting:
        for row in snap["files"]:
            name = row["file_name"][:42]
            keyboard.append([
                btn(f"📄 {name}", f"file:{row['id']}")
            ])

    if selecting:
        keyboard.append([
            btn(
                "✅ الحفظ هنا",
                f"savehere:{folder_id or 0}",
            )
        ])
    else:
        keyboard.append([
            btn("➕ مجلد", f"mk:{folder_id or 0}"),
            btn("⚙️ إدارة المجلد", f"manage:{folder_id or 0}"),
        ])

    nav = []

    if page > 0 and not selecting:
        nav.append(
            btn(
                "⬅️ السابق",
                f"browse:{folder_id or 'root'}:{page - 1}",
            )
        )

    if snap["has_next"] and not selecting:
        nav.append(
            btn(
                "التالي ➡️",
                f"browse:{folder_id or 'root'}:{page + 1}",
            )
        )

    if nav:
        keyboard.append(nav)

    if folder_id is not None:
        parent = snap["folder"]["parent_id"]
        prefix = "pick" if selecting else "browse"
        keyboard.append([
            btn(
                "↩️ رجوع",
                f"{prefix}:{parent or 'root'}:0",
            )
        ])
    else:
        keyboard.append([
            btn("🏠 الرئيسية", "home")
        ])

    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, keyboard)


def pending_screen(chat_id, user_id, message_id=None):
    rows = pending_snapshot(user_id)

    if not rows:
        text = "📥 لا توجد ملفات معلّقة."
        keyboard = [[btn("🏠 الرئيسية", "home")]]

    else:
        text = f"📥 لديك <b>{len(rows)}</b> ملف/ملفات بانتظار التنظيم."

        keyboard = []

        for row in rows[:10]:
            keyboard.append([
                btn(
                    f"📄 {row['file_name'][:38]}",
                    f"pendone:{row['id']}",
                )
            ])

        keyboard.append([
            btn("📚 حفظ الجميع", "pickall:root")
        ])

        keyboard.append([
            btn("🏠 الرئيسية", "home")
        ])

    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, keyboard)


def file_screen(chat_id, user_id, file_id, message_id=None):
    row = get_file(user_id, file_id, saved_only=True)

    if not row:
        text = "❌ الملف غير موجود."
        keyboard = [[btn("📂 ملفاتي", "browse:root:0")]]

    else:
        path = folder_path(user_id, row["folder_id"])

        text = (
            f"📄 <b>{esc(row['file_name'])}</b>\n\n"
            f"📦 {human_size(row['file_size'])}\n"
            f"📂 {esc(path)}\n"
            f"🆔 <code>{row['id']}</code>"
        )

        keyboard = [
            [btn("📤 استرجاع", f"get:{row['id']}")],
            [btn("🗑 حذف", f"delask:{row['id']}")],
            [
                btn(
                    "↩️ المجلد",
                    f"browse:{row['folder_id'] or 'root'}:0",
                )
            ],
        ]

    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, keyboard)


def manage_folder_screen(
    chat_id,
    user_id,
    folder_id,
    message_id,
):
    if not folder_id:
        edit(
            chat_id,
            message_id,
            "📂 هذا هو جذر مكتبتك.",
            [
                [btn("➕ مجلد جديد", "mk:root")],
                [btn("↩️ رجوع", "browse:root:0")],
            ],
        )
        return

    folder = folder_info(user_id, folder_id)

    if not folder:
        edit(
            chat_id,
            message_id,
            "❌ المجلد غير موجود.",
            [[btn("📂 ملفاتي", "browse:root:0")]],
        )
        return

    edit(
        chat_id,
        message_id,
        f"⚙️ <b>{esc(folder['name'])}</b>",
        [
            [btn("✏️ إعادة التسمية", f"ren:{folder_id}")],
            [btn("🗑 حذف المجلد", f"rmdirask:{folder_id}")],
            [btn("↩️ رجوع", f"browse:{folder_id}:0")],
        ],
    )


# ============================================================
# Incoming files
# ============================================================

def handle_file_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]

    ensure_user(user_id)

    f = extract_file(message)

    if not f:
        return

    f["source_chat_id"] = chat_id
    f["source_message_id"] = message.get("message_id")

    duplicate = find_duplicate(
        user_id,
        f["file_unique_id"],
    )

    if duplicate:
        set_state(
            user_id,
            "duplicate",
            duplicate["id"],
            {"new": f},
        )

        path = folder_path(
            user_id,
            duplicate["folder_id"],
        )

        send(
            chat_id,
            "⚠️ <b>هذا الملف موجود بالفعل.</b>\n\n"
            f"📄 {esc(duplicate['file_name'])}\n"
            f"📂 {esc(path)}",
            [
                [btn("📄 فتح الموجود", f"file:{duplicate['id']}")],
                [btn("➕ حفظ نسخة أخرى", "dupkeep")],
                [btn("✖️ إلغاء", "dupcancel")],
            ],
        )
        return

    pending_id = insert_pending(user_id, f)
    count = pending_count(user_id)

    send(
        chat_id,
        f"✅ أضفت <b>{esc(f['file_name'])}</b> إلى <b>الوارد</b>.\n"
        f"📦 {human_size(f['file_size'])}\n\n"
        f"📥 في الوارد الآن: <b>{count}</b>",
        [
            [webapp_btn("📚 فتح الوَرّاق وتنظيم الملفات")],
            [btn("⚙️ الواجهة القديمة", f"pickone:{pending_id}:root")],
        ],
    )


# ============================================================
# Text messages
# ============================================================

def handle_text_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()

    ensure_user(user_id)

    command = None

    if text.startswith("/"):
        command = text.split()[0].split("@")[0].lower()

    if command == "/start":
        clear_state(user_id)
        home_screen(chat_id, user_id)
        return

    if command == "/files":
        clear_state(user_id)
        browse_screen(chat_id, user_id)
        return

    if command == "/search":
        set_state(user_id, "search")
        send(chat_id, "🔎 اكتب اسم الملف أو جزءًا منه:")
        return

    if command == "/help":
        send(
            chat_id,
            "📚 <b>طريقة استخدام الوَرّاق</b>\n\n"
            "• أرسل ملفًا ثم اختر مكان حفظه.\n"
            "• /files لتصفح مكتبتك.\n"
            "• /search للبحث باسم الملف.\n"
            "• تستطيع إنشاء مجلدات داخل مجلدات.\n"
            "• يمكنك إرسال عدة ملفات ثم تنظيمها معًا.",
        )
        return

    state = get_state(user_id)
    action = state["action"] if state else None

    if action == "search":
        rows = search_files(user_id, text)

        if not rows:
            send(
                chat_id,
                f"❌ لم أجد ملفًا يطابق: <b>{esc(text)}</b>",
                [[
                    btn("🔎 بحث جديد", "search"),
                    btn("🏠 الرئيسية", "home"),
                ]],
            )
            return

        keyboard = [
            [
                btn(
                    f"📄 {row['file_name'][:42]}",
                    f"file:{row['id']}",
                )
            ]
            for row in rows
        ]

        keyboard.append([
            btn("🔎 بحث جديد", "search"),
            btn("🏠 الرئيسية", "home"),
        ])

        send(
            chat_id,
            f"🔎 نتائج البحث عن: <b>{esc(text)}</b>\n"
            f"وجدت {len(rows)} نتيجة.",
            keyboard,
        )
        return

    if action == "new_folder":
        parent_id = state["target_id"]

        try:
            new_id = create_folder(
                user_id,
                parent_id,
                text,
            )

            clear_state(user_id)

            send(
                chat_id,
                f"✅ تم إنشاء المجلد: <b>{esc(text)}</b>",
                [[
                    btn(
                        "📂 فتح المجلد",
                        f"browse:{new_id}:0",
                    )
                ]],
            )

        except ValueError as e:
            send(
                chat_id,
                f"❌ {esc(e)}\n\n"
                "أرسل اسمًا آخر أو /start للإلغاء.",
            )

        return

    if action == "rename_folder":
        folder_id = state["target_id"]

        try:
            rename_folder(
                user_id,
                folder_id,
                text,
            )

            clear_state(user_id)

            send(
                chat_id,
                f"✅ تم تغيير الاسم إلى: <b>{esc(text)}</b>",
                [[
                    btn(
                        "📂 فتح المجلد",
                        f"browse:{folder_id}:0",
                    )
                ]],
            )

        except ValueError as e:
            send(
                chat_id,
                f"❌ {esc(e)}\n\n"
                "أرسل اسمًا آخر أو /start للإلغاء.",
            )

        return

    send(
        chat_id,
        "لم أفهم الرسالة 🤔\n"
        "استخدم /start أو أرسل ملفًا.",
    )


# ============================================================
# Callbacks
# ============================================================

def parse_folder_token(token):
    if token in ("root", "0", "", None):
        return None
    return int(token)


def handle_callback(q):
    callback_id = q["id"]
    user_id = q["from"]["id"]

    msg = q.get("message") or {}
    chat_id = msg.get("chat", {}).get("id", user_id)
    message_id = msg.get("message_id")

    data = q.get("data", "")

    # Stop the Telegram spinner immediately before any database work.
    answer_callback(callback_id)

    if data == "home":
        clear_state(user_id)
        home_screen(chat_id, user_id, message_id)
        return

    if data == "search":
        set_state(user_id, "search")

        edit(
            chat_id,
            message_id,
            "🔎 اكتب اسم الملف أو جزءًا منه:",
            [[btn("🏠 إلغاء", "home")]],
        )
        return

    if data == "pending":
        pending_screen(
            chat_id,
            user_id,
            message_id,
        )
        return

    if data.startswith("browse:"):
        _, folder_t, page_t = data.split(":", 2)

        browse_screen(
            chat_id,
            user_id,
            parse_folder_token(folder_t),
            int(page_t),
            message_id,
        )
        return

    if data.startswith("file:"):
        file_screen(
            chat_id,
            user_id,
            int(data.split(":")[1]),
            message_id,
        )
        return

    if data.startswith("get:"):
        file_id = int(data.split(":")[1])
        row = get_file(
            user_id,
            file_id,
            saved_only=True,
        )

        if not row:
            send(chat_id, "❌ الملف غير موجود.")
            return

        try:
            copy_message(
                chat_id,
                STORAGE_CHAT_ID,
                row["storage_message_id"],
            )

        except Exception:
            log.exception("Retrieve failed")
            send(
                chat_id,
                "❌ تعذر استرجاع الملف من المخزن.",
            )

        return

    if data.startswith("delask:"):
        file_id = int(data.split(":")[1])

        row = get_file(
            user_id,
            file_id,
            saved_only=True,
        )

        if not row:
            file_screen(
                chat_id,
                user_id,
                file_id,
                message_id,
            )
            return

        edit(
            chat_id,
            message_id,
            "⚠️ هل تريد حذف هذا الملف نهائيًا؟\n\n"
            f"📄 <b>{esc(row['file_name'])}</b>",
            [[
                btn("🗑 نعم، احذف", f"del:{file_id}"),
                btn("إلغاء", f"file:{file_id}"),
            ]],
        )
        return

    if data.startswith("del:"):
        file_id = int(data.split(":")[1])

        deleted, folder_id = delete_saved_file(
            user_id,
            file_id,
        )

        if deleted:
            edit(
                chat_id,
                message_id,
                "✅ تم حذف الملف.",
                [
                    [
                        btn(
                            "📂 العودة للمجلد",
                            f"browse:{folder_id or 'root'}:0",
                        )
                    ],
                    [btn("🔎 بحث", "search")],
                ],
            )
        else:
            edit(
                chat_id,
                message_id,
                "❌ الملف غير موجود.",
                [[btn("🏠 الرئيسية", "home")]],
            )

        return

    if data.startswith("mk:"):
        parent_t = data.split(":", 1)[1]
        parent_id = parse_folder_token(parent_t)

        set_state(
            user_id,
            "new_folder",
            parent_id,
        )

        path = folder_path(
            user_id,
            parent_id,
        )

        edit(
            chat_id,
            message_id,
            "➕ أرسل اسم المجلد الجديد.\n\n"
            f"📂 داخل: <b>{esc(path)}</b>",
            [[
                btn(
                    "إلغاء",
                    f"browse:{parent_id or 'root'}:0",
                )
            ]],
        )
        return

    if data.startswith("manage:"):
        folder_id = parse_folder_token(
            data.split(":", 1)[1]
        )

        manage_folder_screen(
            chat_id,
            user_id,
            folder_id,
            message_id,
        )
        return

    if data.startswith("ren:"):
        folder_id = int(data.split(":")[1])

        set_state(
            user_id,
            "rename_folder",
            folder_id,
        )

        edit(
            chat_id,
            message_id,
            "✏️ أرسل الاسم الجديد للمجلد:",
            [[
                btn(
                    "إلغاء",
                    f"manage:{folder_id}",
                )
            ]],
        )
        return

    if data.startswith("rmdirask:"):
        folder_id = int(data.split(":")[1])
        folder = folder_info(
            user_id,
            folder_id,
        )

        if not folder:
            home_screen(
                chat_id,
                user_id,
                message_id,
            )
            return

        edit(
            chat_id,
            message_id,
            f"⚠️ حذف المجلد <b>{esc(folder['name'])}</b>؟\n"
            "يجب أن يكون فارغًا.",
            [[
                btn("🗑 نعم", f"rmdir:{folder_id}"),
                btn("إلغاء", f"manage:{folder_id}"),
            ]],
        )
        return

    if data.startswith("rmdir:"):
        folder_id = int(data.split(":")[1])

        try:
            parent_id = remove_empty_folder(
                user_id,
                folder_id,
            )

            edit(
                chat_id,
                message_id,
                "✅ تم حذف المجلد.",
                [[
                    btn(
                        "↩️ رجوع",
                        f"browse:{parent_id or 'root'}:0",
                    )
                ]],
            )

        except ValueError as e:
            edit(
                chat_id,
                message_id,
                f"❌ {esc(e)}",
                [[
                    btn(
                        "↩️ رجوع",
                        f"manage:{folder_id}",
                    )
                ]],
            )

        return

    if data.startswith("pendone:"):
        file_id = int(data.split(":")[1])

        set_state(
            user_id,
            "save_selection",
            None,
            {"ids": [file_id]},
        )

        browse_screen(
            chat_id,
            user_id,
            None,
            0,
            message_id,
            selecting=True,
        )
        return

    if data.startswith("pickone:"):
        _, file_t, folder_t = data.split(":")

        set_state(
            user_id,
            "save_selection",
            None,
            {"ids": [int(file_t)]},
        )

        browse_screen(
            chat_id,
            user_id,
            parse_folder_token(folder_t),
            0,
            message_id,
            selecting=True,
        )
        return

    if data.startswith("pickall:"):
        folder_t = data.split(":", 1)[1]

        ids = [
            row["id"]
            for row in pending_snapshot(user_id)
        ]

        set_state(
            user_id,
            "save_selection",
            None,
            {"ids": ids},
        )

        browse_screen(
            chat_id,
            user_id,
            parse_folder_token(folder_t),
            0,
            message_id,
            selecting=True,
        )
        return

    if data.startswith("pick:"):
        parts = data.split(":")
        folder_id = parse_folder_token(parts[1])

        browse_screen(
            chat_id,
            user_id,
            folder_id,
            0,
            message_id,
            selecting=True,
        )
        return

    if data.startswith("savehere:"):
        folder_id = parse_folder_token(
            data.split(":", 1)[1]
        )

        state = get_state(user_id)

        if (
            not state
            or state["action"] != "save_selection"
        ):
            edit(
                chat_id,
                message_id,
                "❌ انتهت جلسة الاختيار. اختر الملفات من جديد.",
                [[btn("📥 الملفات المعلقة", "pending")]],
            )
            return

        state_data = state["data"] or {}
        ids = state_data.get("ids") or []

        if not ids:
            edit(
                chat_id,
                message_id,
                "❌ لا توجد ملفات محددة.",
                [[btn("📥 الملفات المعلقة", "pending")]],
            )
            return

        try:
            saved = save_many(
                user_id,
                ids,
                folder_id,
            )

            clear_state(user_id)

            path = folder_path(
                user_id,
                folder_id,
            )

            edit(
                chat_id,
                message_id,
                f"✅ تم حفظ <b>{len(saved)}</b> ملف/ملفات.\n"
                f"📂 {esc(path)}",
                [
                    [
                        btn(
                            "📂 فتح المجلد",
                            f"browse:{folder_id or 'root'}:0",
                        )
                    ],
                    [btn("🏠 الرئيسية", "home")],
                ],
            )

        except Exception:
            log.exception("Saving failed")

            edit(
                chat_id,
                message_id,
                "❌ حدث خطأ أثناء الحفظ.\n"
                "بقيت الملفات غير المكتملة معلّقة؛ جرّب مجددًا.",
                [[btn("📥 الملفات المعلقة", "pending")]],
            )

        return

    if data == "dupkeep":
        state = get_state(user_id)

        if (
            not state
            or state["action"] != "duplicate"
        ):
            home_screen(
                chat_id,
                user_id,
                message_id,
            )
            return

        state_data = state["data"] or {}
        new_file = state_data.get("new")

        if not new_file:
            clear_state(user_id)
            home_screen(
                chat_id,
                user_id,
                message_id,
            )
            return

        file_id = insert_pending(
            user_id,
            new_file,
        )

        set_state(
            user_id,
            "save_selection",
            None,
            {"ids": [file_id]},
        )

        browse_screen(
            chat_id,
            user_id,
            None,
            0,
            message_id,
            selecting=True,
        )
        return

    if data == "dupcancel":
        clear_state(user_id)

        edit(
            chat_id,
            message_id,
            "✖️ أُلغي حفظ النسخة الجديدة.",
            [[btn("🏠 الرئيسية", "home")]],
        )
        return


# ============================================================
# Public entry point used by app.py
# ============================================================

def handle_update(update):
    try:
        ensure_db()

        if "callback_query" in update:
            handle_callback(
                update["callback_query"]
            )

        elif "message" in update:
            message = update["message"]

            if extract_file(message):
                handle_file_message(message)

            elif message.get("text"):
                handle_text_message(message)

        return {"ok": True}

    except Exception as e:
        log.exception("Alwarraq update failed")
        return {
            "ok": False,
            "error": str(e),
        }


# Compatibility with current app.py
handle_warraq_update = handle_update
process_update = handle_update
