import os
import requests
import psycopg
from psycopg.rows import dict_row


WARRAQ_BOT_TOKEN = os.environ.get("WARRAQ_BOT_TOKEN")
WARRAQ_STORAGE_CHAT_ID = os.environ.get("WARRAQ_STORAGE_CHAT_ID")
WARRAQ_DATABASE_URL = os.environ.get("WARRAQ_DATABASE_URL")

PAGE_SIZE = 8


# ============================================================
# Database
# ============================================================

def get_db():
    return psycopg.connect(
        WARRAQ_DATABASE_URL,
        row_factory=dict_row
    )


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:

            # الجدول الأساسي - للتركيبات الجديدة
            cur.execute("""
                CREATE TABLE IF NOT EXISTS files (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    filename TEXT NOT NULL,
                    file_size BIGINT NOT NULL DEFAULT 0,
                    telegram_file_id TEXT,
                    telegram_file_unique_id TEXT,
                    source_chat_id BIGINT,
                    source_message_id BIGINT,
                    storage_message_id BIGINT,
                    folder TEXT NOT NULL DEFAULT 'pending',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    deleted_at TIMESTAMPTZ
                )
            """)

            # Migration للجدول الموجود عندنا حاليًا
            cur.execute("""
                ALTER TABLE files
                ADD COLUMN IF NOT EXISTS telegram_file_unique_id TEXT
            """)

            cur.execute("""
                ALTER TABLE files
                ADD COLUMN IF NOT EXISTS source_chat_id BIGINT
            """)

            cur.execute("""
                ALTER TABLE files
                ADD COLUMN IF NOT EXISTS source_message_id BIGINT
            """)

            cur.execute("""
                ALTER TABLE files
                ADD COLUMN IF NOT EXISTS status TEXT
                NOT NULL DEFAULT 'saved'
            """)

            cur.execute("""
                ALTER TABLE files
                ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ
            """)

            # pending لا يملك storage_message_id بعد
            cur.execute("""
                ALTER TABLE files
                ALTER COLUMN storage_message_id DROP NOT NULL
            """)

            # فهارس
            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_files_user_status
                ON files(user_id, status)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_files_user_folder
                ON files(user_id, folder, status)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS
                idx_files_filename_lower
                ON files(LOWER(filename))
            """)

            # منع إنشاء سجلين لنفس رسالة المستخدم
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_files_unique_source
                ON files(
                    user_id,
                    source_chat_id,
                    source_message_id
                )
                WHERE
                    source_chat_id IS NOT NULL
                    AND source_message_id IS NOT NULL
            """)


def create_pending_file(
    user_id,
    filename,
    file_size,
    telegram_file_id,
    telegram_file_unique_id,
    source_chat_id,
    source_message_id
):
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO files (
                    user_id,
                    filename,
                    file_size,
                    telegram_file_id,
                    telegram_file_unique_id,
                    source_chat_id,
                    source_message_id,
                    folder,
                    status
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, 'pending', 'pending'
                )
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (
                    user_id,
                    filename,
                    file_size,
                    telegram_file_id,
                    telegram_file_unique_id,
                    source_chat_id,
                    source_message_id
                )
            )

            row = cur.fetchone()

            if row:
                return row["id"]

            # وصل نفس Update مرة أخرى مثلًا
            cur.execute(
                """
                SELECT id
                FROM files
                WHERE user_id = %s
                  AND source_chat_id = %s
                  AND source_message_id = %s
                LIMIT 1
                """,
                (
                    user_id,
                    source_chat_id,
                    source_message_id
                )
            )

            row = cur.fetchone()

            return row["id"] if row else None


def claim_pending(file_id, user_id, folder):
    """
    يحوّل pending -> saving بشكل ذري.
    يمنع الضغط مرتين من إنشاء نسختين.
    """

    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE files
                SET status = 'saving',
                    folder = %s
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'pending'
                RETURNING *
                """,
                (
                    folder,
                    file_id,
                    user_id
                )
            )

            return cur.fetchone()


def revert_pending(file_id, user_id):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE files
                SET status = 'pending',
                    folder = 'pending'
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'saving'
                """,
                (
                    file_id,
                    user_id
                )
            )


def finish_saved(
    file_id,
    user_id,
    storage_message_id
):
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE files
                SET status = 'saved',
                    storage_message_id = %s
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'saving'
                RETURNING id
                """,
                (
                    storage_message_id,
                    file_id,
                    user_id
                )
            )

            return cur.fetchone()


def get_file(file_id, user_id):
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM files
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'saved'
                LIMIT 1
                """,
                (
                    file_id,
                    user_id
                )
            )

            return cur.fetchone()


def get_folder_counts(user_id):
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT folder, COUNT(*) AS count
                FROM files
                WHERE user_id = %s
                  AND status = 'saved'
                GROUP BY folder
                """,
                (user_id,)
            )

            return {
                row["folder"]: row["count"]
                for row in cur.fetchall()
            }


def get_files_page(
    user_id,
    folder,
    page
):
    offset = page * PAGE_SIZE

    with get_db() as conn:
        with conn.cursor() as cur:

            if folder == "all":

                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM files
                    WHERE user_id = %s
                      AND status = 'saved'
                    """,
                    (user_id,)
                )

                total = cur.fetchone()["total"]

                cur.execute(
                    """
                    SELECT *
                    FROM files
                    WHERE user_id = %s
                      AND status = 'saved'
                    ORDER BY id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (
                        user_id,
                        PAGE_SIZE,
                        offset
                    )
                )

            else:

                cur.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM files
                    WHERE user_id = %s
                      AND status = 'saved'
                      AND folder = %s
                    """,
                    (
                        user_id,
                        folder
                    )
                )

                total = cur.fetchone()["total"]

                cur.execute(
                    """
                    SELECT *
                    FROM files
                    WHERE user_id = %s
                      AND status = 'saved'
                      AND folder = %s
                    ORDER BY id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (
                        user_id,
                        folder,
                        PAGE_SIZE,
                        offset
                    )
                )

            rows = cur.fetchall()

            return rows, total


def search_files(user_id, query):
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM files
                WHERE user_id = %s
                  AND status = 'saved'
                  AND filename ILIKE %s
                ORDER BY id DESC
                LIMIT 10
                """,
                (
                    user_id,
                    f"%{query}%"
                )
            )

            return cur.fetchall()


def mark_deleted(file_id, user_id):
    with get_db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                UPDATE files
                SET status = 'deleted',
                    deleted_at = NOW()
                WHERE id = %s
                  AND user_id = %s
                  AND status = 'saved'
                RETURNING id
                """,
                (
                    file_id,
                    user_id
                )
            )

            return cur.fetchone()


# ============================================================
# Telegram
# ============================================================

def telegram_api(method, data=None):
    url = (
        f"https://api.telegram.org/"
        f"bot{WARRAQ_BOT_TOKEN}/{method}"
    )

    return requests.post(
        url,
        json=data or {},
        timeout=30
    )


def send_message(
    chat_id,
    text,
    reply_markup=None
):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    return telegram_api(
        "sendMessage",
        payload
    )


def answer_callback(
    callback_id,
    text=None
):
    payload = {
        "callback_query_id": callback_id
    }

    if text:
        payload["text"] = text

    return telegram_api(
        "answerCallbackQuery",
        payload
    )


# ============================================================
# Helpers
# ============================================================

FOLDERS = {
    "books": "📚 الكتب",
    "university": "🎓 الجامعة",
    "lectures": "🎧 المحاضرات",
    "projects": "🛠 المشاريع",
    "other": "📦 أخرى",
    "uncategorized": "📥 بلا تصنيف"
}


def human_size(size):
    if not size:
        return "0 B"

    if size < 1024:
        return f"{size} B"

    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"

    if size < 1024 ** 3:
        return f"{size / 1024 ** 2:.2f} MB"

    return f"{size / 1024 ** 3:.2f} GB"


def short_name(name, length=45):
    if len(name) <= length:
        return name

    return name[:length - 1] + "…"


# ============================================================
# Keyboards
# ============================================================

def main_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📁 ملفاتي",
                    "callback_data": "files"
                },
                {
                    "text": "🔎 بحث",
                    "callback_data": "search"
                }
            ]
        ]
    }


def folder_keyboard(file_id):
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📚 الكتب",
                    "callback_data":
                        f"save:books:{file_id}"
                },
                {
                    "text": "🎓 الجامعة",
                    "callback_data":
                        f"save:university:{file_id}"
                }
            ],
            [
                {
                    "text": "🎧 المحاضرات",
                    "callback_data":
                        f"save:lectures:{file_id}"
                },
                {
                    "text": "🛠 المشاريع",
                    "callback_data":
                        f"save:projects:{file_id}"
                }
            ],
            [
                {
                    "text": "📦 أخرى",
                    "callback_data":
                        f"save:other:{file_id}"
                },
                {
                    "text": "⚡ بلا تصنيف",
                    "callback_data":
                        f"save:uncategorized:{file_id}"
                }
            ]
        ]
    }


def files_folders_keyboard(counts):

    buttons = []

    for code, name in FOLDERS.items():
        count = counts.get(code, 0)

        buttons.append({
            "text": f"{name} ({count})",
            "callback_data": f"folder:{code}:0"
        })

    rows = []

    for i in range(0, len(buttons), 2):
        rows.append(
            buttons[i:i + 2]
        )

    total = sum(counts.values())

    rows.insert(
        0,
        [
            {
                "text":
                    f"🗂 كل الملفات ({total})",
                "callback_data":
                    "folder:all:0"
            }
        ]
    )

    return {
        "inline_keyboard": rows
    }


def file_list_keyboard(
    rows,
    folder,
    page,
    total
):
    keyboard = []

    for row in rows:

        keyboard.append([
            {
                "text":
                    "📄 "
                    + short_name(row["filename"]),
                "callback_data":
                    f"file:{row['id']}"
            }
        ])

    nav = []

    if page > 0:
        nav.append({
            "text": "⬅️ السابق",
            "callback_data":
                f"folder:{folder}:{page - 1}"
        })

    if (page + 1) * PAGE_SIZE < total:
        nav.append({
            "text": "التالي ➡️",
            "callback_data":
                f"folder:{folder}:{page + 1}"
        })

    if nav:
        keyboard.append(nav)

    keyboard.append([
        {
            "text": "↩️ الأقسام",
            "callback_data": "files"
        }
    ])

    return {
        "inline_keyboard": keyboard
    }


def file_actions_keyboard(file_id):
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📥 استرجاع",
                    "callback_data":
                        f"get:{file_id}"
                }
            ],
            [
                {
                    "text": "🗑 حذف",
                    "callback_data":
                        f"deleteask:{file_id}"
                }
            ],
            [
                {
                    "text": "↩️ ملفاتي",
                    "callback_data": "files"
                }
            ]
        ]
    }


# ============================================================
# UI
# ============================================================

def show_main_menu(chat_id):
    send_message(
        chat_id,
        (
            "📚 الوَرّاق\n\n"
            "أرسل ملفًا لحفظه، "
            "أو استخدم الخيارات:"
        ),
        main_keyboard()
    )


def show_folders(chat_id, user_id):
    counts = get_folder_counts(user_id)

    send_message(
        chat_id,
        "📁 ملفاتي\n\nاختر القسم:",
        files_folders_keyboard(counts)
    )


def show_folder(
    chat_id,
    user_id,
    folder,
    page
):
    rows, total = get_files_page(
        user_id,
        folder,
        page
    )

    if not rows:
        send_message(
            chat_id,
            "📭 لا توجد ملفات هنا.",
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "↩️ الأقسام",
                            "callback_data": "files"
                        }
                    ]
                ]
            }
        )
        return

    if folder == "all":
        title = "🗂 كل الملفات"
    else:
        title = FOLDERS.get(
            folder,
            folder
        )

    send_message(
        chat_id,
        (
            f"{title}\n\n"
            f"عدد الملفات: {total}"
        ),
        file_list_keyboard(
            rows,
            folder,
            page,
            total
        )
    )


def show_file(
    chat_id,
    user_id,
    file_id
):
    row = get_file(
        file_id,
        user_id
    )

    if not row:
        send_message(
            chat_id,
            "❌ الملف غير موجود."
        )
        return

    folder_name = FOLDERS.get(
        row["folder"],
        row["folder"]
    )

    text = (
        f"📄 {row['filename']}\n\n"
        f"📦 {human_size(row['file_size'])}\n"
        f"📁 {folder_name}\n"
        f"🆔 {row['id']}"
    )

    send_message(
        chat_id,
        text,
        file_actions_keyboard(
            row["id"]
        )
    )


def show_search_results(
    chat_id,
    user_id,
    query
):
    rows = search_files(
        user_id,
        query
    )

    if not rows:
        send_message(
            chat_id,
            "🔎 لم أجد ملفًا بهذا الاسم."
        )
        return

    keyboard = []

    for row in rows:
        keyboard.append([
            {
                "text":
                    "📄 "
                    + short_name(row["filename"]),
                "callback_data":
                    f"file:{row['id']}"
            }
        ])

    send_message(
        chat_id,
        (
            f"🔎 نتائج البحث عن:\n"
            f"{query}\n\n"
            f"وجدت {len(rows)} نتيجة."
        ),
        {
            "inline_keyboard":
                keyboard
        }
    )


# ============================================================
# Upload
# ============================================================

def handle_document(message):

    chat = message.get(
        "chat",
        {}
    )

    # لأسباب الخصوصية: خزانة شخصية فقط
    if chat.get("type") != "private":
        return

    chat_id = chat.get("id")

    user_id = message.get(
        "from",
        {}
    ).get("id")

    document = message["document"]

    filename = document.get(
        "file_name",
        "ملف"
    )

    file_size = document.get(
        "file_size",
        0
    )

    telegram_file_id = document.get(
        "file_id"
    )

    unique_id = document.get(
        "file_unique_id"
    )

    source_message_id = message.get(
        "message_id"
    )

    file_id = create_pending_file(
        user_id=user_id,
        filename=filename,
        file_size=file_size,
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=unique_id,
        source_chat_id=chat_id,
        source_message_id=source_message_id
    )

    if not file_id:
        send_message(
            chat_id,
            "❌ تعذر إنشاء سجل للملف."
        )
        return

    send_message(
        chat_id,
        (
            f"📄 {filename}\n"
            f"📦 {human_size(file_size)}\n\n"
            "أين تريد حفظ الملف؟"
        ),
        folder_keyboard(file_id)
    )


# ============================================================
# Saving
# ============================================================

def save_pending_file(
    chat_id,
    user_id,
    callback_id,
    file_id,
    folder
):

    row = claim_pending(
        file_id,
        user_id,
        folder
    )

    if not row:
        answer_callback(
            callback_id,
            "هذا الملف حُفظ أو جارٍ حفظه بالفعل."
        )
        return

    answer_callback(
        callback_id,
        "⏳ جارٍ الحفظ..."
    )

    folder_name = FOLDERS.get(
        folder,
        folder
    )

    caption = (
        f"📚 الوَرّاق\n\n"
        f"📁 {folder_name}\n"
        f"👤 المالك: {user_id}\n"
        f"🆔 {file_id}\n\n"
        f"#warraq "
        f"#u_{user_id} "
        f"#f_{folder}"
    )

    response = telegram_api(
        "copyMessage",
        {
            "chat_id":
                WARRAQ_STORAGE_CHAT_ID,

            "from_chat_id":
                row["source_chat_id"],

            "message_id":
                row["source_message_id"],

            "caption":
                caption
        }
    )

    if not response.ok:

        revert_pending(
            file_id,
            user_id
        )

        print(
            "[WARRAQ COPY ERROR]",
            response.status_code,
            response.text,
            flush=True
        )

        send_message(
            chat_id,
            "❌ تعذر حفظ الملف."
        )

        return

    result = response.json().get(
        "result",
        {}
    )

    storage_message_id = result.get(
        "message_id"
    )

    try:
        saved = finish_saved(
            file_id,
            user_id,
            storage_message_id
        )

        if not saved:
            raise RuntimeError(
                "Database save failed"
            )

    except Exception as e:

        # لا نترك ملفًا يتيمًا بالقناة
        telegram_api(
            "deleteMessage",
            {
                "chat_id":
                    WARRAQ_STORAGE_CHAT_ID,
                "message_id":
                    storage_message_id
            }
        )

        revert_pending(
            file_id,
            user_id
        )

        print(
            "[WARRAQ DATABASE ERROR]",
            repr(e),
            flush=True
        )

        send_message(
            chat_id,
            "❌ فشل تسجيل الملف في الفهرس."
        )

        return

    send_message(
        chat_id,
        (
            "✅ تم حفظ الملف في الوَرّاق.\n\n"
            f"📁 {folder_name}\n"
            f"🆔 {file_id}"
        ),
        main_keyboard()
    )


# ============================================================
# Retrieval
# ============================================================

def retrieve_file(
    chat_id,
    user_id,
    file_id
):
    row = get_file(
        file_id,
        user_id
    )

    if not row:
        send_message(
            chat_id,
            "❌ الملف غير موجود."
        )
        return

    response = telegram_api(
        "copyMessage",
        {
            "chat_id": chat_id,
            "from_chat_id":
                WARRAQ_STORAGE_CHAT_ID,
            "message_id":
                row["storage_message_id"],
            "caption":
                f"📚 من الوَرّاق\n"
                f"🆔 {row['id']}"
        }
    )

    if not response.ok:

        print(
            "[WARRAQ RETRIEVE ERROR]",
            response.status_code,
            response.text,
            flush=True
        )

        send_message(
            chat_id,
            "❌ تعذر استرجاع الملف."
        )


# ============================================================
# Delete
# ============================================================

def ask_delete(
    chat_id,
    user_id,
    file_id
):
    row = get_file(
        file_id,
        user_id
    )

    if not row:
        send_message(
            chat_id,
            "❌ الملف غير موجود."
        )
        return

    send_message(
        chat_id,
        (
            "⚠️ هل تريد حذف هذا الملف نهائيًا؟\n\n"
            f"📄 {row['filename']}"
        ),
        {
            "inline_keyboard": [
                [
                    {
                        "text": "🗑 نعم، احذف",
                        "callback_data":
                            f"delete:{file_id}"
                    },
                    {
                        "text": "إلغاء",
                        "callback_data":
                            f"file:{file_id}"
                    }
                ]
            ]
        }
    )


def delete_file(
    chat_id,
    user_id,
    file_id
):
    row = get_file(
        file_id,
        user_id
    )

    if not row:
        send_message(
            chat_id,
            "❌ الملف غير موجود."
        )
        return

    response = telegram_api(
        "deleteMessage",
        {
            "chat_id":
                WARRAQ_STORAGE_CHAT_ID,
            "message_id":
                row["storage_message_id"]
        }
    )

    if not response.ok:

        print(
            "[WARRAQ DELETE ERROR]",
            response.status_code,
            response.text,
            flush=True
        )

        send_message(
            chat_id,
            "❌ تعذر حذف الملف من المخزن."
        )

        return

    deleted = mark_deleted(
        file_id,
        user_id
    )

    if not deleted:
        send_message(
            chat_id,
            "⚠️ حُذف الملف من المخزن، "
            "لكن حدث خطأ في الفهرس."
        )
        return

    send_message(
        chat_id,
        "✅ تم حذف الملف.",
        main_keyboard()
    )


# ============================================================
# Messages
# ============================================================

def handle_message(message):

    chat = message.get(
        "chat",
        {}
    )

    if chat.get("type") != "private":
        return

    chat_id = chat.get("id")

    user_id = message.get(
        "from",
        {}
    ).get("id")

    text = message.get(
        "text",
        ""
    )


    if "document" in message:
        handle_document(message)
        return


    if text.startswith("/start"):
        show_main_menu(chat_id)
        return


    if text.startswith("/files"):
        show_folders(
            chat_id,
            user_id
        )
        return


    if text.startswith("/search"):

        query = text[
            len("/search"):
        ].strip()

        if query:
            show_search_results(
                chat_id,
                user_id,
                query
            )
        else:
            send_message(
                chat_id,
                "🔎 اكتب اسم الملف الذي تريد البحث عنه:",
                {
                    "force_reply": True,
                    "selective": True,
                    "input_field_placeholder":
                        "اسم الملف..."
                }
            )

        return


    # رد على طلب البحث
    reply_to = message.get(
        "reply_to_message",
        {}
    )

    reply_text = reply_to.get(
        "text",
        ""
    )

    if (
        text
        and reply_text.startswith(
            "🔎 اكتب اسم الملف"
        )
    ):
        show_search_results(
            chat_id,
            user_id,
            text.strip()
        )
        return


    show_main_menu(chat_id)


# ============================================================
# Callbacks
# ============================================================

def handle_callback(callback):

    callback_id = callback.get("id")

    user_id = callback.get(
        "from",
        {}
    ).get("id")

    message = callback.get(
        "message",
        {}
    )

    chat_id = message.get(
        "chat",
        {}
    ).get("id")

    data = callback.get(
        "data",
        ""
    )


    # ------------------ FILES ------------------

    if data == "files":

        answer_callback(
            callback_id
        )

        show_folders(
            chat_id,
            user_id
        )

        return


    # ------------------ SEARCH ------------------

    if data == "search":

        answer_callback(
            callback_id
        )

        send_message(
            chat_id,
            "🔎 اكتب اسم الملف الذي تريد البحث عنه:",
            {
                "force_reply": True,
                "selective": True,
                "input_field_placeholder":
                    "اسم الملف..."
            }
        )

        return


    # ------------------ SAVE ------------------

    if data.startswith("save:"):

        try:
            _, folder, file_id = (
                data.split(":")
            )

            file_id = int(file_id)

        except Exception:
            answer_callback(
                callback_id,
                "❌ زر غير صالح."
            )
            return

        save_pending_file(
            chat_id,
            user_id,
            callback_id,
            file_id,
            folder
        )

        return


    # ------------------ FOLDER ------------------

    if data.startswith("folder:"):

        try:
            _, folder, page = (
                data.split(":")
            )

            page = int(page)

        except Exception:
            answer_callback(
                callback_id,
                "❌ زر غير صالح."
            )
            return

        answer_callback(
            callback_id
        )

        show_folder(
            chat_id,
            user_id,
            folder,
            page
        )

        return


    # ------------------ FILE DETAILS ------------------

    if data.startswith("file:"):

        try:
            file_id = int(
                data.split(":")[1]
            )

        except Exception:
            answer_callback(
                callback_id,
                "❌ زر غير صالح."
            )
            return

        answer_callback(
            callback_id
        )

        show_file(
            chat_id,
            user_id,
            file_id
        )

        return


    # ------------------ GET ------------------

    if data.startswith("get:"):

        try:
            file_id = int(
                data.split(":")[1]
            )

        except Exception:
            answer_callback(
                callback_id,
                "❌ زر غير صالح."
            )
            return

        answer_callback(
            callback_id,
            "📥 جارٍ الاسترجاع..."
        )

        retrieve_file(
            chat_id,
            user_id,
            file_id
        )

        return


    # ------------------ DELETE ASK ------------------

    if data.startswith("deleteask:"):

        try:
            file_id = int(
                data.split(":")[1]
            )

        except Exception:
            answer_callback(
                callback_id,
                "❌ زر غير صالح."
            )
            return

        answer_callback(
            callback_id
        )

        ask_delete(
            chat_id,
            user_id,
            file_id
        )

        return


    # ------------------ DELETE ------------------

    if
