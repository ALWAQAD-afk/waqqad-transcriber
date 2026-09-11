import os
import json
import html
import logging
from contextlib import contextmanager

import requests
import psycopg
from psycopg.rows import dict_row

# ============================================================
# Alwarraq v1.1 RC
# Telegram file library backed by a private Telegram channel
# Metadata/state backed by Neon PostgreSQL
# ============================================================

BOT_TOKEN = os.getenv("WARRAQ_BOT_TOKEN", "").strip()
STORAGE_CHAT_ID = os.getenv("WARRAQ_STORAGE_CHAT_ID", "").strip()
DATABASE_URL = os.getenv("WARRAQ_DATABASE_URL", "").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
PAGE_SIZE = 8

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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("alwarraq")


# ============================================================
# Database
# ============================================================

@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("WARRAQ_DATABASE_URL is not configured")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Safe, repeatable schema creation/migration."""
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                name TEXT NOT NULL,
                icon TEXT NOT NULL DEFAULT '📁',
                parent_id BIGINT REFERENCES folders(id) ON DELETE RESTRICT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS folders_unique_root
            ON folders(user_id, lower(name))
            WHERE parent_id IS NULL
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS folders_unique_child
            ON folders(user_id, parent_id, lower(name))
            WHERE parent_id IS NOT NULL
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                telegram_file_id TEXT,
                file_unique_id TEXT,
                file_name TEXT NOT NULL DEFAULT 'Telegram file',
                file_size BIGINT NOT NULL DEFAULT 0,
                mime_type TEXT,
                storage_message_id BIGINT,
                folder_id BIGINT REFERENCES folders(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                saved_at TIMESTAMPTZ
            )
        """)

        # Migrate the older v1 table without destroying data.
        migrations = [
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS telegram_file_id TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS file_unique_id TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS file_name TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS file_size BIGINT DEFAULT 0",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS mime_type TEXT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS storage_message_id BIGINT",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS folder_id BIGINT REFERENCES folders(id) ON DELETE SET NULL",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'saved'",
            "ALTER TABLE files ADD COLUMN IF NOT EXISTS saved_at TIMESTAMPTZ",
        ]
        for sql in migrations:
            conn.execute(sql)

        # Compatibility with the earlier v1 schema used by this project.
        cols = {
            r["column_name"]
            for r in conn.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema='public' AND table_name='files'
            """).fetchall()
        }

        # Old releases used filename / telegram_file_unique_id / folder.
        if "filename" in cols:
            conn.execute("""
                UPDATE files
                SET file_name = COALESCE(NULLIF(file_name, ''), filename, 'Telegram file')
                WHERE file_name IS NULL OR file_name = '' OR file_name = 'Telegram file'
            """)
            conn.execute("ALTER TABLE files ALTER COLUMN filename DROP NOT NULL")

        if "telegram_file_unique_id" in cols:
            conn.execute("""
                UPDATE files
                SET file_unique_id = COALESCE(file_unique_id, telegram_file_unique_id)
                WHERE file_unique_id IS NULL
            """)

        if "folder" in cols:
            conn.execute("ALTER TABLE files ALTER COLUMN folder DROP NOT NULL")

        # Pending records do not have a storage message yet.
        conn.execute("ALTER TABLE files ALTER COLUMN storage_message_id DROP NOT NULL")

        conn.execute("""
            UPDATE files
            SET file_name = COALESCE(NULLIF(file_name, ''), 'Telegram file')
            WHERE file_name IS NULL OR file_name = ''
        """)
        conn.execute("""
            UPDATE files SET status='saved'
            WHERE status IS NULL
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS files_user_folder_idx
            ON files(user_id, folder_id, id DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS files_user_name_idx
            ON files(user_id, lower(file_name))
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS files_unique_idx
            ON files(user_id, file_unique_id)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id BIGINT PRIMARY KEY,
                action TEXT,
                target_id BIGINT,
                data JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)


_db_ready = False


def ensure_db():
    global _db_ready
    if not _db_ready:
        init_db()
        _db_ready = True


# ============================================================
# Telegram helpers
# ============================================================

def tg(method, payload=None, timeout=30):
    if not BOT_TOKEN:
        raise RuntimeError("WARRAQ_BOT_TOKEN is not configured")
    r = requests.post(f"{API}/{method}", json=payload or {}, timeout=timeout)
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
    payload = {"callback_query_id": callback_id, "show_alert": alert}
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
        tg("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
    except Exception:
        log.exception("Could not delete Telegram message")


def kb_button(text, data):
    return {"text": text, "callback_data": data}


def esc(s):
    return html.escape(str(s or ""))


def human_size(n):
    n = int(n or 0)
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / (1024 ** 2):.2f} MB"
    return f"{n / (1024 ** 3):.2f} GB"


# ============================================================
# User/state/folder helpers
# ============================================================

def ensure_user(user_id):
    ensure_db()
    with db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM folders WHERE user_id=%s",
            (user_id,),
        ).fetchone()["n"]
        if count == 0:
            for name, icon in DEFAULT_FOLDERS:
                conn.execute(
                    """
                    INSERT INTO folders(user_id,name,icon,parent_id)
                    VALUES(%s,%s,%s,NULL)
                    ON CONFLICT DO NOTHING
                    """,
                    (user_id, name, icon),
                )


def set_state(user_id, action=None, target_id=None, data=None):
    with db() as conn:
        conn.execute("""
            INSERT INTO user_states(user_id,action,target_id,data,updated_at)
            VALUES(%s,%s,%s,%s::jsonb,NOW())
            ON CONFLICT(user_id) DO UPDATE SET
                action=EXCLUDED.action,
                target_id=EXCLUDED.target_id,
                data=EXCLUDED.data,
                updated_at=NOW()
        """, (user_id, action, target_id, json.dumps(data or {})))


def get_state(user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM user_states WHERE user_id=%s",
            (user_id,),
        ).fetchone()


def clear_state(user_id):
    set_state(user_id, None, None, {})


def get_folder(user_id, folder_id):
    if folder_id is None:
        return None
    with db() as conn:
        return conn.execute(
            "SELECT * FROM folders WHERE id=%s AND user_id=%s",
            (folder_id, user_id),
        ).fetchone()


def folder_path(user_id, folder_id):
    names = []
    seen = set()
    current = folder_id
    with db() as conn:
        while current and current not in seen:
            seen.add(current)
            row = conn.execute(
                "SELECT id,name,parent_id FROM folders WHERE id=%s AND user_id=%s",
                (current, user_id),
            ).fetchone()
            if not row:
                break
            names.append(row["name"])
            current = row["parent_id"]
    return " / ".join(reversed(names)) or "ملفاتي"


def list_children(user_id, parent_id):
    with db() as conn:
        if parent_id is None:
            return conn.execute("""
                SELECT * FROM folders
                WHERE user_id=%s AND parent_id IS NULL
                ORDER BY id
            """, (user_id,)).fetchall()
        return conn.execute("""
            SELECT * FROM folders
            WHERE user_id=%s AND parent_id=%s
            ORDER BY lower(name), id
        """, (user_id, parent_id)).fetchall()


def parent_of(user_id, folder_id):
    row = get_folder(user_id, folder_id)
    return row["parent_id"] if row else None


def create_folder(user_id, parent_id, name):
    name = " ".join((name or "").strip().split())
    if not name:
        raise ValueError("اسم المجلد فارغ.")
    if len(name) > 60:
        raise ValueError("اسم المجلد طويل جدًا.")
    if "/" in name:
        raise ValueError("لا تستخدم / داخل اسم المجلد.")
    if parent_id is not None and not get_folder(user_id, parent_id):
        raise ValueError("المجلد الأب غير موجود.")

    with db() as conn:
        if parent_id is None:
            exists = conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s AND parent_id IS NULL AND lower(name)=lower(%s)
            """, (user_id, name)).fetchone()
        else:
            exists = conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s AND parent_id=%s AND lower(name)=lower(%s)
            """, (user_id, parent_id, name)).fetchone()
        if exists:
            raise ValueError("يوجد مجلد بهذا الاسم هنا بالفعل.")

        return conn.execute("""
            INSERT INTO folders(user_id,name,icon,parent_id)
            VALUES(%s,%s,'📁',%s)
            RETURNING id
        """, (user_id, name, parent_id)).fetchone()["id"]


def rename_folder(user_id, folder_id, name):
    folder = get_folder(user_id, folder_id)
    if not folder:
        raise ValueError("المجلد غير موجود.")
    name = " ".join((name or "").strip().split())
    if not name or len(name) > 60 or "/" in name:
        raise ValueError("اسم المجلد غير صالح.")

    with db() as conn:
        if folder["parent_id"] is None:
            exists = conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s AND parent_id IS NULL
                  AND lower(name)=lower(%s) AND id<>%s
            """, (user_id, name, folder_id)).fetchone()
        else:
            exists = conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s AND parent_id=%s
                  AND lower(name)=lower(%s) AND id<>%s
            """, (user_id, folder["parent_id"], name, folder_id)).fetchone()
        if exists:
            raise ValueError("يوجد مجلد بهذا الاسم هنا بالفعل.")
        conn.execute(
            "UPDATE folders SET name=%s WHERE id=%s AND user_id=%s",
            (name, folder_id, user_id),
        )


def remove_empty_folder(user_id, folder_id):
    folder = get_folder(user_id, folder_id)
    if not folder:
        raise ValueError("المجلد غير موجود.")
    with db() as conn:
        child = conn.execute(
            "SELECT 1 FROM folders WHERE user_id=%s AND parent_id=%s LIMIT 1",
            (user_id, folder_id),
        ).fetchone()
        file_ = conn.execute(
            "SELECT 1 FROM files WHERE user_id=%s AND folder_id=%s AND status='saved' LIMIT 1",
            (user_id, folder_id),
        ).fetchone()
        if child or file_:
            raise ValueError("المجلد غير فارغ. انقل/احذف محتواه أولًا.")
        conn.execute(
            "DELETE FROM folders WHERE id=%s AND user_id=%s",
            (folder_id, user_id),
        )


# ============================================================
# File helpers
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
            ext = {
                "audio": "Audio",
                "video": "Video",
                "voice": "Voice message",
                "animation": "Animation",
            }.get(kind, "Telegram file")
            return {
                "telegram_file_id": obj.get("file_id"),
                "file_unique_id": obj.get("file_unique_id"),
                "file_name": obj.get("file_name") or ext,
                "file_size": obj.get("file_size") or 0,
                "mime_type": obj.get("mime_type"),
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
        }
    return None


def pending_files(user_id):
    with db() as conn:
        return conn.execute("""
            SELECT * FROM files
            WHERE user_id=%s AND status='pending'
            ORDER BY id
        """, (user_id,)).fetchall()


def saved_duplicate(user_id, unique_id):
    if not unique_id:
        return None
    with db() as conn:
        return conn.execute("""
            SELECT * FROM files
            WHERE user_id=%s AND file_unique_id=%s AND status='saved'
            ORDER BY id DESC LIMIT 1
        """, (user_id, unique_id)).fetchone()


def insert_pending(user_id, f):
    with db() as conn:
        return conn.execute("""
            INSERT INTO files(
                user_id,telegram_file_id,file_unique_id,file_name,
                file_size,mime_type,status
            )
            VALUES(%s,%s,%s,%s,%s,%s,'pending')
            RETURNING id
        """, (
            user_id, f["telegram_file_id"], f["file_unique_id"], f["file_name"],
            f["file_size"], f["mime_type"],
        )).fetchone()["id"]


def get_file(user_id, file_id, saved_only=False):
    sql = "SELECT * FROM files WHERE id=%s AND user_id=%s"
    params = [file_id, user_id]
    if saved_only:
        sql += " AND status='saved'"
    with db() as conn:
        return conn.execute(sql, params).fetchone()


def files_in_folder(user_id, folder_id, page=0):
    offset = max(0, page) * PAGE_SIZE
    with db() as conn:
        if folder_id is None:
            rows = conn.execute("""
                SELECT * FROM files
                WHERE user_id=%s AND folder_id IS NULL AND status='saved'
                ORDER BY id DESC LIMIT %s OFFSET %s
            """, (user_id, PAGE_SIZE + 1, offset)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM files
                WHERE user_id=%s AND folder_id=%s AND status='saved'
                ORDER BY id DESC LIMIT %s OFFSET %s
            """, (user_id, folder_id, PAGE_SIZE + 1, offset)).fetchall()
    return rows[:PAGE_SIZE], len(rows) > PAGE_SIZE


def search_files(user_id, query, limit=20):
    q = f"%{query.strip()}%"
    with db() as conn:
        return conn.execute("""
            SELECT * FROM files
            WHERE user_id=%s AND status='saved'
              AND file_name ILIKE %s
            ORDER BY id DESC LIMIT %s
        """, (user_id, q, limit)).fetchall()


def save_one_direct(user_id, pending_id, folder_id):
    """
    Store a pending Telegram document by file_id.
    Kept separate to avoid relying on the original source message.
    """
    if folder_id is not None and not get_folder(user_id, folder_id):
        raise ValueError("المجلد غير موجود.")

    with db() as conn:
        row = conn.execute("""
            UPDATE files SET status='saving'
            WHERE id=%s AND user_id=%s AND status='pending'
            RETURNING *
        """, (pending_id, user_id)).fetchone()
    if not row:
        return False

    caption = (
        f"📚 <b>الوَرّاق</b>\n\n"
        f"📄 {esc(row['file_name'])}\n"
        f"📦 {human_size(row['file_size'])}\n"
        f"👤 المالك: <code>{user_id}</code>\n"
        f"#warraq #u_{user_id}"
    )
    try:
        result = tg("sendDocument", {
            "chat_id": STORAGE_CHAT_ID,
            "document": row["telegram_file_id"],
            "caption": caption,
            "parse_mode": "HTML",
        }, timeout=60)
    except Exception:
        with db() as conn:
            conn.execute(
                "UPDATE files SET status='pending' WHERE id=%s AND user_id=%s",
                (pending_id, user_id),
            )
        raise

    with db() as conn:
        conn.execute("""
            UPDATE files
            SET storage_message_id=%s,folder_id=%s,status='saved',saved_at=NOW()
            WHERE id=%s AND user_id=%s
        """, (result["message_id"], folder_id, pending_id, user_id))
    return True


def save_many_direct(user_id, ids, folder_id):
    out = []
    for fid in ids:
        if save_one_direct(user_id, fid, folder_id):
            out.append(fid)
    return out


def move_saved_file(user_id, file_id, folder_id):
    if folder_id is not None and not get_folder(user_id, folder_id):
        raise ValueError("المجلد غير موجود.")
    with db() as conn:
        row = conn.execute("""
            UPDATE files
            SET folder_id=%s
            WHERE id=%s AND user_id=%s AND status='saved'
            RETURNING id
        """, (folder_id, file_id, user_id)).fetchone()
    return bool(row)


def delete_saved_file(user_id, file_id):
    row = get_file(user_id, file_id, saved_only=True)
    if not row:
        return False
    if row["storage_message_id"]:
        delete_message(STORAGE_CHAT_ID, row["storage_message_id"])
    with db() as conn:
        conn.execute(
            "DELETE FROM files WHERE id=%s AND user_id=%s",
            (file_id, user_id),
        )
    return True


# ============================================================
# Screens
# ============================================================

def home_screen(chat_id, user_id, message_id=None):
    ensure_user(user_id)
    pending = len(pending_files(user_id))
    text = (
        "📚 <b>الوَرّاق</b>\n\n"
        "مكتبتك الخاصة داخل تيليجرام.\n"
        "أرسل إليّ ملفًا لحفظه، أو استخدم الخيارات:"
    )
    if pending:
        text += f"\n\n📥 لديك <b>{pending}</b> ملف/ملفات بانتظار التنظيم."
    keyboard = [
        [kb_button("📂 ملفاتي", "browse:root:0"), kb_button("🔎 بحث", "search")],
        [kb_button("➕ مجلد جديد", "mk:root")],
    ]
    if pending:
        keyboard.append([kb_button(f"📚 تنظيم المعلّق ({pending})", "pending")])
    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, keyboard)


def browse_screen(chat_id, user_id, folder_id=None, page=0, message_id=None, selecting=False):
    ensure_user(user_id)
    children = list_children(user_id, folder_id)
    files_, has_next = files_in_folder(user_id, folder_id, page)
    path = folder_path(user_id, folder_id)

    text = f"📂 <b>{esc(path)}</b>\n"
    if selecting:
        state = get_state(user_id)
        action = state["action"] if state else None
        if action == "move_file":
            text += "\nاختر المجلد الجديد للملف."
        else:
            count = len((state or {}).get("data", {}).get("ids", [])) if state else 0
            text += f"\nاختر مكان الحفظ{f' لـ {count} ملف/ملفات' if count else ''}."
    elif not children and not files_:
        text += "\nهذا المجلد فارغ."

    keyboard = []
    for f in children:
        mode = "pick" if selecting else "browse"
        keyboard.append([
            kb_button(f"{f['icon']} {f['name']}", f"{mode}:{f['id']}")
        ])

    if not selecting:
        for f in files_:
            keyboard.append([
                kb_button(f"📄 {f['file_name'][:42]}", f"file:{f['id']}")
            ])

    if selecting:
        keyboard.append([kb_button("✅ اختيار هذا المجلد", f"choosefolder:{folder_id or 0}")])
    else:
        keyboard.append([
            kb_button("➕ مجلد", f"mk:{folder_id or 0}"),
            kb_button("⚙️ إدارة المجلد", f"manage:{folder_id or 0}"),
        ])

    nav = []
    if page > 0 and not selecting:
        nav.append(kb_button("⬅️ السابق", f"browse:{folder_id or 'root'}:{page-1}"))
    if has_next and not selecting:
        nav.append(kb_button("التالي ➡️", f"browse:{folder_id or 'root'}:{page+1}"))
    if nav:
        keyboard.append(nav)

    if folder_id is not None:
        parent = parent_of(user_id, folder_id)
        if selecting:
            keyboard.append([kb_button("↩️ رجوع", f"pick:{parent or 'root'}")])
        else:
            keyboard.append([kb_button("↩️ رجوع", f"browse:{parent or 'root'}:0")])
    else:
        keyboard.append([kb_button("🏠 الرئيسية", "home")])

    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, keyboard)

def file_screen(chat_id, user_id, file_id, message_id=None):
    row = get_file(user_id, file_id, saved_only=True)
    if not row:
        text = "❌ الملف غير موجود."
        keyboard = [[kb_button("📂 ملفاتي", "browse:root:0")]]
    else:
        path = folder_path(user_id, row["folder_id"])
        text = (
            f"📄 <b>{esc(row['file_name'])}</b>\n\n"
            f"📦 {human_size(row['file_size'])}\n"
            f"📂 {esc(path)}\n"
            f"🆔 <code>{row['id']}</code>"
        )
        keyboard = [
            [kb_button("📤 استرجاع", f"get:{row['id']}")],
            [kb_button("📂 نقل", f"move:{row['id']}"), kb_button("🗑 حذف", f"delask:{row['id']}")],
            [kb_button("↩️ المجلد", f"browse:{row['folder_id'] or 'root'}:0")],
        ]
    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, keyboard)


def pending_screen(chat_id, user_id, message_id=None):
    rows = pending_files(user_id)
    if not rows:
        text = "📥 لا توجد ملفات معلّقة."
        keyboard = [[kb_button("🏠 الرئيسية", "home")]]
    else:
        text = f"📥 لديك <b>{len(rows)}</b> ملف/ملفات بانتظار التنظيم."
        keyboard = []
        for r in rows[:10]:
            keyboard.append([
                kb_button(f"📄 {r['file_name'][:38]}", f"pendone:{r['id']}")
            ])
        keyboard.append([kb_button("📚 حفظ الجميع", "pickall:root")])
        keyboard.append([kb_button("🏠 الرئيسية", "home")])
    if message_id:
        edit(chat_id, message_id, text, keyboard)
    else:
        send(chat_id, text, keyboard)


def manage_folder_screen(chat_id, user_id, folder_id, message_id):
    if not folder_id:
        edit(chat_id, message_id, "📂 هذا هو جذر مكتبتك.", [
            [kb_button("➕ مجلد جديد", "mk:root")],
            [kb_button("↩️ رجوع", "browse:root:0")],
        ])
        return
    folder = get_folder(user_id, folder_id)
    if not folder:
        edit(chat_id, message_id, "❌ المجلد غير موجود.", [[kb_button("📂 ملفاتي", "browse:root:0")]])
        return
    edit(chat_id, message_id, f"⚙️ <b>{esc(folder['name'])}</b>", [
        [kb_button("✏️ إعادة التسمية", f"ren:{folder_id}")],
        [kb_button("🗑 حذف المجلد", f"rmdirask:{folder_id}")],
        [kb_button("↩️ رجوع", f"browse:{folder_id}:0")],
    ])


# ============================================================
# Incoming message handling
# ============================================================

def handle_file_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    ensure_user(user_id)

    f = extract_file(message)
    if not f:
        return

    dup = saved_duplicate(user_id, f["file_unique_id"])
    if dup:
        set_state(user_id, "duplicate", dup["id"], {"new": f})
        send(chat_id,
             "⚠️ <b>هذا الملف موجود بالفعل.</b>\n\n"
             f"📄 {esc(dup['file_name'])}\n"
             f"📂 {esc(folder_path(user_id, dup['folder_id']))}",
             [
                 [kb_button("📄 فتح الموجود", f"file:{dup['id']}")],
                 [kb_button("➕ حفظ نسخة أخرى", "dupkeep")],
                 [kb_button("✖️ إلغاء", "dupcancel")],
             ])
        return

    pending_id = insert_pending(user_id, f)
    count = len(pending_files(user_id))
    send(chat_id,
         f"✅ استلمت <b>{esc(f['file_name'])}</b>.\n"
         f"📦 {human_size(f['file_size'])}\n\n"
         f"📥 المعلّق الآن: <b>{count}</b>",
         [
             [kb_button("📁 حفظ هذا الملف", f"pickone:{pending_id}:root")],
             [kb_button(f"📚 تنظيم كل المعلّق ({count})", "pending")],
         ])


def handle_text_message(message):
    user_id = message["from"]["id"]
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    ensure_user(user_id)

    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
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
        send(chat_id,
             "📚 <b>طريقة استخدام الوَرّاق</b>\n\n"
             "• أرسل ملفًا ثم اختر مكان حفظه.\n"
             "• /files لتصفح مكتبتك.\n"
             "• /search للبحث باسم الملف.\n"
             "• تستطيع إنشاء مجلدات داخل مجلدات.\n"
             "• يمكنك إرسال عدة ملفات ثم تنظيمها معًا.")
        return

    state = get_state(user_id)
    action = state["action"] if state else None

    if action == "search":
        if not text:
            send(chat_id, "اكتب كلمة للبحث.")
            return
        rows = search_files(user_id, text)
        if not rows:
            send(chat_id, f"❌ لم أجد ملفًا يطابق: <b>{esc(text)}</b>",
                 [[kb_button("🔎 بحث جديد", "search"), kb_button("🏠 الرئيسية", "home")]])
            return
        keyboard = [[kb_button(f"📄 {r['file_name'][:42]}", f"file:{r['id']}")] for r in rows]
        keyboard.append([kb_button("🔎 بحث جديد", "search"), kb_button("🏠 الرئيسية", "home")])
        send(chat_id, f"🔎 نتائج البحث عن: <b>{esc(text)}</b>\nوجدت {len(rows)} نتيجة.", keyboard)
        return

    if action == "new_folder":
        parent_id = state["target_id"]
        try:
            new_id = create_folder(user_id, parent_id, text)
            clear_state(user_id)
            send(chat_id, f"✅ تم إنشاء المجلد: <b>{esc(text)}</b>",
                 [[kb_button("📂 فتح المجلد", f"browse:{new_id}:0")]])
        except ValueError as e:
            send(chat_id, f"❌ {esc(e)}\n\nأرسل اسمًا آخر أو /start للإلغاء.")
        return

    if action == "rename_folder":
        folder_id = state["target_id"]
        try:
            rename_folder(user_id, folder_id, text)
            clear_state(user_id)
            send(chat_id, f"✅ تم تغيير الاسم إلى: <b>{esc(text)}</b>",
                 [[kb_button("📂 فتح المجلد", f"browse:{folder_id}:0")]])
        except ValueError as e:
            send(chat_id, f"❌ {esc(e)}\n\nأرسل اسمًا آخر أو /start للإلغاء.")
        return

    send(chat_id, "لم أفهم الرسالة 🤔\nاستخدم /start أو أرسل ملفًا.")


# ============================================================
# Callback handling
# ============================================================

def parse_folder_token(token):
    return None if token in ("root", "0", "", None) else int(token)


def handle_callback(q):
    callback_id = q["id"]
    user_id = q["from"]["id"]
    msg = q.get("message") or {}
    chat_id = msg.get("chat", {}).get("id", user_id)
    message_id = msg.get("message_id")
    data = q.get("data", "")
    ensure_user(user_id)
    answer_callback(callback_id)

    if data == "home":
        clear_state(user_id)
        home_screen(chat_id, user_id, message_id)
        return

    if data == "search":
        set_state(user_id, "search")
        edit(chat_id, message_id, "🔎 اكتب اسم الملف أو جزءًا منه:",
             [[kb_button("🏠 إلغاء", "home")]])
        return

    if data == "pending":
        pending_screen(chat_id, user_id, message_id)
        return

    if data.startswith("browse:"):
        _, folder_t, page_t = data.split(":", 2)
        browse_screen(chat_id, user_id, parse_folder_token(folder_t), int(page_t), message_id)
        return

    if data.startswith("file:"):
        file_screen(chat_id, user_id, int(data.split(":")[1]), message_id)
        return

    if data.startswith("get:"):
        fid = int(data.split(":")[1])
        row = get_file(user_id, fid, saved_only=True)
        if not row:
            answer_callback(callback_id, "الملف غير موجود.", True)
            return
        try:
            copy_message(chat_id, STORAGE_CHAT_ID, row["storage_message_id"])
        except Exception:
            send(chat_id, "❌ تعذر استرجاع الملف من المخزن.")
        return

    if data.startswith("delask:"):
        fid = int(data.split(":")[1])
        row = get_file(user_id, fid, saved_only=True)
        if not row:
            file_screen(chat_id, user_id, fid, message_id)
            return
        edit(chat_id, message_id,
             f"⚠️ هل تريد حذف هذا الملف نهائيًا؟\n\n📄 <b>{esc(row['file_name'])}</b>",
             [
                 [kb_button("🗑 نعم، احذف", f"del:{fid}"), kb_button("إلغاء", f"file:{fid}")],
             ])
        return

    if data.startswith("del:"):
        fid = int(data.split(":")[1])
        row = get_file(user_id, fid, saved_only=True)
        folder_id = row["folder_id"] if row else None
        if delete_saved_file(user_id, fid):
            edit(chat_id, message_id, "✅ تم حذف الملف.", [
                [kb_button("📂 العودة للمجلد", f"browse:{folder_id or 'root'}:0")],
                [kb_button("🔎 بحث", "search")],
            ])
        else:
            edit(chat_id, message_id, "❌ الملف غير موجود.", [[kb_button("🏠 الرئيسية", "home")]])
        return

    if data.startswith("mk:"):
        parent_t = data.split(":", 1)[1]
        parent_id = parse_folder_token(parent_t)
        set_state(user_id, "new_folder", parent_id)
        edit(chat_id, message_id,
             f"➕ أرسل اسم المجلد الجديد.\n\n📂 داخل: <b>{esc(folder_path(user_id, parent_id))}</b>",
             [[kb_button("إلغاء", f"browse:{parent_id or 'root'}:0")]])
        return

    if data.startswith("manage:"):
        folder_id = parse_folder_token(data.split(":", 1)[1])
        manage_folder_screen(chat_id, user_id, folder_id, message_id)
        return

    if data.startswith("ren:"):
        folder_id = int(data.split(":")[1])
        set_state(user_id, "rename_folder", folder_id)
        edit(chat_id, message_id, "✏️ أرسل الاسم الجديد للمجلد:",
             [[kb_button("إلغاء", f"manage:{folder_id}")]])
        return

    if data.startswith("rmdirask:"):
        folder_id = int(data.split(":")[1])
        folder = get_folder(user_id, folder_id)
        if not folder:
            home_screen(chat_id, user_id, message_id)
            return
        edit(chat_id, message_id,
             f"⚠️ حذف المجلد <b>{esc(folder['name'])}</b>؟\n"
             "يجب أن يكون فارغًا.",
             [[kb_button("🗑 نعم", f"rmdir:{folder_id}"),
               kb_button("إلغاء", f"manage:{folder_id}")]])
        return

    if data.startswith("rmdir:"):
        folder_id = int(data.split(":")[1])
        folder = get_folder(user_id, folder_id)
        parent = folder["parent_id"] if folder else None
        try:
            remove_empty_folder(user_id, folder_id)
            edit(chat_id, message_id, "✅ تم حذف المجلد.",
                 [[kb_button("↩️ رجوع", f"browse:{parent or 'root'}:0")]])
        except ValueError as e:
            edit(chat_id, message_id, f"❌ {esc(e)}",
                 [[kb_button("↩️ رجوع", f"manage:{folder_id}")]])
        return

    if data.startswith("move:"):
        fid = int(data.split(":")[1])
        row = get_file(user_id, fid, saved_only=True)
        if not row:
            file_screen(chat_id, user_id, fid, message_id)
            return
        set_state(user_id, "move_file", fid, {})
        browse_screen(chat_id, user_id, None, 0, message_id, True)
        return

    if data.startswith("pendone:"):
        fid = int(data.split(":")[1])
        set_state(user_id, "save_files", None, {"ids": [fid]})
        browse_screen(chat_id, user_id, None, 0, message_id, True)
        return

    if data.startswith("pickone:"):
        _, fid_t, folder_t = data.split(":")
        set_state(user_id, "save_files", None, {"ids": [int(fid_t)]})
        browse_screen(chat_id, user_id, parse_folder_token(folder_t), 0, message_id, True)
        return

    if data.startswith("pickall:"):
        folder_t = data.split(":", 1)[1]
        ids = [r["id"] for r in pending_files(user_id)]
        set_state(user_id, "save_files", None, {"ids": ids})
        browse_screen(chat_id, user_id, parse_folder_token(folder_t), 0, message_id, True)
        return

    if data.startswith("pick:"):
        folder_t = data.split(":", 1)[1]
        browse_screen(chat_id, user_id, parse_folder_token(folder_t), 0, message_id, True)
        return

    if data.startswith("choosefolder:"):
        folder_id = parse_folder_token(data.split(":", 1)[1])
        state = get_state(user_id)
        action = state["action"] if state else None

        if action == "save_files":
            ids = (state["data"] or {}).get("ids", [])
            try:
                saved = save_many_direct(user_id, ids, folder_id)
                clear_state(user_id)
                edit(chat_id, message_id,
                     f"✅ تم حفظ <b>{len(saved)}</b> ملف/ملفات.\n"
                     f"📂 {esc(folder_path(user_id, folder_id))}",
                     [
                         [kb_button("📂 فتح المجلد", f"browse:{folder_id or 'root'}:0")],
                         [kb_button("🏠 الرئيسية", "home")],
                     ])
            except Exception:
                log.exception("Saving failed")
                edit(chat_id, message_id,
                     "❌ حدث خطأ أثناء الحفظ. بقيت الملفات غير المكتملة معلّقة؛ جرّب مجددًا.",
                     [[kb_button("📥 الملفات المعلقة", "pending")]])
            return

        if action == "move_file":
            fid = state["target_id"]
            try:
                ok = move_saved_file(user_id, fid, folder_id)
                clear_state(user_id)
                if ok:
                    edit(chat_id, message_id,
                         f"✅ تم نقل الملف إلى:\n📂 {esc(folder_path(user_id, folder_id))}",
                         [[kb_button("📄 فتح الملف", f"file:{fid}"),
                           kb_button("📂 فتح المجلد", f"browse:{folder_id or 'root'}:0")]])
                else:
                    edit(chat_id, message_id, "❌ الملف غير موجود.",
                         [[kb_button("🏠 الرئيسية", "home")]])
            except ValueError as e:
                edit(chat_id, message_id, f"❌ {esc(e)}",
                     [[kb_button("🏠 الرئيسية", "home")]])
            return

        home_screen(chat_id, user_id, message_id)
        return

    if data == "dupkeep":
        state = get_state(user_id)
        if not state or state["action"] != "duplicate":
            home_screen(chat_id, user_id, message_id)
            return
        raw = state["data"]
        newf = raw.get("new") if isinstance(raw, dict) else None
        if not newf:
            clear_state(user_id)
            home_screen(chat_id, user_id, message_id)
            return
        fid = insert_pending(user_id, newf)
        set_state(user_id, "save_files", None, {"ids": [fid]})
        browse_screen(chat_id, user_id, None, 0, message_id, True)
        return

    if data == "dupcancel":
        clear_state(user_id)
        edit(chat_id, message_id, "✖️ أُلغي حفظ النسخة الجديدة.",
             [[kb_button("🏠 الرئيسية", "home")]])
        return


# ============================================================
# Public entry point for Flask app.py
# ============================================================

def handle_update(update):
    """
    Call this from app.py's POST /telegram/warraq route.
    Returns quickly with a JSON-serializable dict.
    """
    try:
        ensure_db()
        if "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "message" in update:
            message = update["message"]
            if extract_file(message):
                handle_file_message(message)
            elif message.get("text"):
                handle_text_message(message)
        return {"ok": True}
    except Exception as e:
        log.exception("Alwarraq update failed")
        return {"ok": False, "error": str(e)}


# Compatibility aliases for likely older app.py names.
handle_warraq_update = handle_update
process_update = handle_update
