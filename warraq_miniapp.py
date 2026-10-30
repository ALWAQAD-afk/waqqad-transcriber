import os
import hmac
import json
import time
import hashlib
from urllib.parse import parse_qsl

from flask import request, jsonify, send_file

from warraq import (
    BOT_TOKEN,
    STORAGE_CHAT_ID,
    ensure_db,
    ensure_user,
    run_tx,
    human_size,
    save_one,
    get_file,
    copy_message,
    tg,
    MINIAPP_URL,
    retrieve_saved_file_to_user,
)


# ============================================================
# Telegram Mini App authentication
# ============================================================

INIT_DATA_MAX_AGE = int(os.getenv("TELEGRAM_INITDATA_MAX_AGE", "86400"))


def _verify_init_data(init_data: str, max_age=None):
    if max_age is None:
        max_age = INIT_DATA_MAX_AGE
    if not init_data:
        raise ValueError("Missing Telegram initData")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = pairs.pop("hash", None)

    if not their_hash:
        raise ValueError("Missing Telegram hash")

    data_check_string = "\n".join(
        f"{key}={pairs[key]}" for key in sorted(pairs)
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, their_hash):
        raise ValueError("Invalid Telegram signature")

    now = int(time.time())
    auth_date = int(pairs.get("auth_date", "0") or 0)
    if (
        not auth_date
        or now - auth_date > max_age
        or auth_date - now > 300
    ):
        raise ValueError("Expired Telegram session")

    user = json.loads(pairs.get("user") or "{}")
    if not user.get("id"):
        raise ValueError("Missing Telegram user")

    return user


def _current_user():
    return _verify_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )


def _error(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


# ============================================================
# Serialization helpers
# ============================================================

def _folder_paths(conn, user_id):
    rows = conn.execute("""
        WITH RECURSIVE paths AS (
            SELECT id,name,parent_id,name::text AS path
            FROM folders
            WHERE user_id=%s AND parent_id IS NULL

            UNION ALL

            SELECT
                f.id,
                f.name,
                f.parent_id,
                (p.path || ' / ' || f.name)::text
            FROM folders f
            JOIN paths p ON f.parent_id=p.id
            WHERE f.user_id=%s
        )
        SELECT id,path FROM paths
    """, (user_id, user_id)).fetchall()

    return {row["id"]: row["path"] for row in rows}


def _kind(row):
    media = (row.get("media_type") or "").lower()
    mime = (row.get("mime_type") or "").lower()
    name = (row.get("file_name") or "").lower()

    if media == "photo" or mime.startswith("image/"):
        return "image"

    if media in ("audio", "voice") or mime.startswith("audio/"):
        return "audio"

    if media == "video" or mime.startswith("video/"):
        return "video"

    if name.endswith(".pdf") or mime == "application/pdf":
        return "pdf"

    if (
        name.endswith((".doc", ".docx"))
        or mime in (
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    ):
        return "word"

    if (
        name.endswith((".txt", ".md", ".csv", ".json", ".rtf"))
        or mime.startswith("text/")
    ):
        return "text"

    if name.endswith((".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz")):
        return "archive"

    return "document"


def _serialize_file(row, paths=None):
    folder_id = row.get("folder_id")

    return {
        "id": row["id"],
        "name": row.get("file_name") or "Telegram file",
        "size": int(row.get("file_size") or 0),
        "size_human": human_size(row.get("file_size") or 0),
        "mime_type": row.get("mime_type"),
        "media_type": row.get("media_type"),
        "kind": _kind(row),
        "folder_id": folder_id,
        "path": (paths or {}).get(
            folder_id,
            "ملفاتي" if folder_id is None else "",
        ),
        "status": row.get("status"),
    }


# ============================================================
# Data loaders
# ============================================================

def _all_folders(user_id):
    def work(conn):
        rows = conn.execute("""
            SELECT id,name,icon,parent_id
            FROM folders
            WHERE user_id=%s
            ORDER BY parent_id NULLS FIRST, lower(name), id
        """, (user_id,)).fetchall()

        return [dict(row) for row in rows]

    return run_tx(work)


def _bootstrap(user_id):
    def work(conn):
        paths = _folder_paths(conn, user_id)

        folders = conn.execute("""
            SELECT id,name,icon,parent_id
            FROM folders
            WHERE user_id=%s
            ORDER BY parent_id NULLS FIRST, lower(name), id
        """, (user_id,)).fetchall()

        inbox_count = conn.execute("""
            SELECT COUNT(*) AS n
            FROM files
            WHERE user_id=%s AND status='pending'
        """, (user_id,)).fetchone()["n"]

        total_files = conn.execute("""
            SELECT COUNT(*) AS n
            FROM files
            WHERE user_id=%s AND status='saved'
        """, (user_id,)).fetchone()["n"]

        recent = conn.execute("""
            SELECT
                id,file_name,file_size,mime_type,media_type,
                folder_id,status
            FROM files
            WHERE user_id=%s AND status='saved'
            ORDER BY COALESCE(saved_at,created_at) DESC,id DESC
            LIMIT 12
        """, (user_id,)).fetchall()

        return {
            "folders": [dict(row) for row in folders],
            "inbox_count": inbox_count,
            "total_files": total_files,
            "recent": [
                _serialize_file(row, paths)
                for row in recent
            ],
        }

    return run_tx(work)


def _folder_contents(user_id, folder_id):
    def work(conn):
        paths = _folder_paths(conn, user_id)

        if folder_id is not None:
            exists = conn.execute("""
                SELECT id
                FROM folders
                WHERE user_id=%s AND id=%s
            """, (user_id, folder_id)).fetchone()

            if not exists:
                raise ValueError("المجلد غير موجود")

        if folder_id is None:
            files = conn.execute("""
                SELECT
                    id,file_name,file_size,mime_type,media_type,
                    folder_id,status
                FROM files
                WHERE user_id=%s
                  AND status='saved'
                  AND folder_id IS NULL
                ORDER BY id DESC
            """, (user_id,)).fetchall()

            breadcrumb = []

        else:
            files = conn.execute("""
                SELECT
                    id,file_name,file_size,mime_type,media_type,
                    folder_id,status
                FROM files
                WHERE user_id=%s
                  AND status='saved'
                  AND folder_id=%s
                ORDER BY id DESC
            """, (user_id, folder_id)).fetchall()

            breadcrumb = conn.execute("""
                WITH RECURSIVE tree AS (
                    SELECT id,name,parent_id,1 AS depth
                    FROM folders
                    WHERE user_id=%s AND id=%s

                    UNION ALL

                    SELECT f.id,f.name,f.parent_id,t.depth+1
                    FROM folders f
                    JOIN tree t ON f.id=t.parent_id
                    WHERE f.user_id=%s
                )
                SELECT id,name,parent_id
                FROM tree
                ORDER BY depth DESC
            """, (user_id, folder_id, user_id)).fetchall()

        return {
            "files": [
                _serialize_file(row, paths)
                for row in files
            ],
            "breadcrumb": [dict(row) for row in breadcrumb],
        }

    return run_tx(work)


def _inbox(user_id):
    def work(conn):
        rows = conn.execute("""
            SELECT
                id,file_name,file_size,mime_type,media_type,
                folder_id,status
            FROM files
            WHERE user_id=%s AND status='pending'
            ORDER BY id DESC
        """, (user_id,)).fetchall()

        return [_serialize_file(row) for row in rows]

    return run_tx(work)


def _search(user_id, query, kind):
    def work(conn):
        paths = _folder_paths(conn, user_id)

        rows = conn.execute("""
            SELECT
                id,file_name,file_size,mime_type,media_type,
                folder_id,status
            FROM files
            WHERE user_id=%s
              AND status='saved'
              AND (%s='' OR file_name ILIKE %s)
            ORDER BY id DESC
            LIMIT 150
        """, (user_id, query, f"%{query}%")).fetchall()

        result = [
            _serialize_file(row, paths)
            for row in rows
        ]

        if kind and kind != "all":
            result = [
                row for row in result
                if row["kind"] == kind
            ]

        return result

    return run_tx(work)


# ============================================================
# Folder mutations
# ============================================================

def _normalize_folder_name(name):
    name = " ".join((name or "").strip().split())

    if not name:
        raise ValueError("اكتب اسم المجلد")

    if len(name) > 60:
        raise ValueError("اسم المجلد طويل جدًا")

    if "/" in name:
        raise ValueError("لا تستخدم / داخل اسم المجلد")

    return name


def _folder_exists(conn, user_id, folder_id):
    if folder_id is None:
        return True

    return bool(
        conn.execute("""
            SELECT 1
            FROM folders
            WHERE user_id=%s AND id=%s
        """, (user_id, folder_id)).fetchone()
    )


def _same_name_exists(conn, user_id, parent_id, name, exclude_id=None):
    if parent_id is None:
        sql = """
            SELECT 1
            FROM folders
            WHERE user_id=%s
              AND parent_id IS NULL
              AND lower(name)=lower(%s)
        """
        params = [user_id, name]
    else:
        sql = """
            SELECT 1
            FROM folders
            WHERE user_id=%s
              AND parent_id=%s
              AND lower(name)=lower(%s)
        """
        params = [user_id, parent_id, name]

    if exclude_id is not None:
        sql += " AND id<>%s"
        params.append(exclude_id)

    return bool(conn.execute(sql, params).fetchone())


def _create_folder(user_id, name, parent_id):
    name = _normalize_folder_name(name)

    def work(conn):
        if not _folder_exists(conn, user_id, parent_id):
            raise ValueError("المجلد الأب غير موجود")

        if _same_name_exists(conn, user_id, parent_id, name):
            raise ValueError("يوجد مجلد بهذا الاسم هنا")

        row = conn.execute("""
            INSERT INTO folders(user_id,name,icon,parent_id)
            VALUES(%s,%s,'📁',%s)
            RETURNING id,name,icon,parent_id
        """, (user_id, name, parent_id)).fetchone()

        return dict(row)

    return run_tx(work)


def _rename_folder(user_id, folder_id, name):
    name = _normalize_folder_name(name)

    def work(conn):
        folder = conn.execute("""
            SELECT id,parent_id
            FROM folders
            WHERE user_id=%s AND id=%s
        """, (user_id, folder_id)).fetchone()

        if not folder:
            raise ValueError("المجلد غير موجود")

        if _same_name_exists(
            conn,
            user_id,
            folder["parent_id"],
            name,
            exclude_id=folder_id,
        ):
            raise ValueError("يوجد مجلد بهذا الاسم هنا")

        conn.execute("""
            UPDATE folders
            SET name=%s
            WHERE user_id=%s AND id=%s
        """, (name, user_id, folder_id))

    run_tx(work)


def _delete_folder(user_id, folder_id):
    def work(conn):
        folder = conn.execute("""
            SELECT id,parent_id
            FROM folders
            WHERE user_id=%s AND id=%s
        """, (user_id, folder_id)).fetchone()

        if not folder:
            raise ValueError("المجلد غير موجود")

        has_child = conn.execute("""
            SELECT 1
            FROM folders
            WHERE user_id=%s AND parent_id=%s
            LIMIT 1
        """, (user_id, folder_id)).fetchone()

        has_file = conn.execute("""
            SELECT 1
            FROM files
            WHERE user_id=%s
              AND folder_id=%s
              AND status IN ('saved','pending','saving')
            LIMIT 1
        """, (user_id, folder_id)).fetchone()

        if has_child or has_file:
            raise ValueError("المجلد غير فارغ")

        conn.execute("""
            DELETE FROM folders
            WHERE user_id=%s AND id=%s
        """, (user_id, folder_id))

        return folder["parent_id"]

    return run_tx(work)


# ============================================================
# File mutations
# ============================================================

def _move_files(user_id, file_ids, folder_id):
    ids = [int(x) for x in file_ids]

    if not ids:
        raise ValueError("لم تحدد أي ملف")

    def work(conn):
        if not _folder_exists(conn, user_id, folder_id):
            raise ValueError("المجلد غير موجود")

        rows = conn.execute("""
            UPDATE files
            SET folder_id=%s
            WHERE user_id=%s
              AND status='saved'
              AND id=ANY(%s)
            RETURNING id
        """, (folder_id, user_id, ids)).fetchall()

        return len(rows)

    return run_tx(work)


def _delete_saved_file(user_id, file_id):
    row = get_file(
        user_id,
        file_id,
        saved_only=True,
    )

    if not row:
        raise ValueError("الملف غير موجود")

    storage_message_id = row.get("storage_message_id")

    if storage_message_id:
        tg(
            "deleteMessage",
            {
                "chat_id": STORAGE_CHAT_ID,
                "message_id": storage_message_id,
            },
        )

    def work(conn):
        conn.execute("""
            DELETE FROM files
            WHERE user_id=%s AND id=%s
        """, (user_id, file_id))

    run_tx(work)


# ============================================================
# Flask routes
# ============================================================

def register_warraq_miniapp(app):
    @app.route("/warraq", methods=["GET"])
    def warraq_page():
        return send_file(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "warraq.html",
            )
        )

    @app.route("/warraq/api/bootstrap", methods=["GET"])
    def api_bootstrap():
        try:
            ensure_db()
            user = _current_user()
            ensure_user(user["id"])

            return jsonify({
                "ok": True,
                "user": user,
                **_bootstrap(user["id"]),
            })

        except ValueError as e:
            return _error(e, 401)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/tree", methods=["GET"])
    def api_tree():
        try:
            ensure_db()
            user = _current_user()

            return jsonify({
                "ok": True,
                "folders": _all_folders(user["id"]),
            })

        except ValueError as e:
            return _error(e, 401)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/folder", methods=["GET"])
    def api_folder():
        try:
            ensure_db()
            user = _current_user()

            raw = request.args.get("id")
            folder_id = (
                int(raw)
                if raw not in (None, "", "root", "0")
                else None
            )

            return jsonify({
                "ok": True,
                **_folder_contents(
                    user["id"],
                    folder_id,
                ),
            })

        except ValueError as e:
            return _error(e, 400)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/inbox", methods=["GET"])
    def api_inbox():
        try:
            ensure_db()
            user = _current_user()

            return jsonify({
                "ok": True,
                "files": _inbox(user["id"]),
            })

        except ValueError as e:
            return _error(e, 401)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/search", methods=["GET"])
    def api_search():
        try:
            ensure_db()
            user = _current_user()

            query = (
                request.args.get("q")
                or ""
            ).strip()

            kind = (
                request.args.get("kind")
                or "all"
            ).strip()

            return jsonify({
                "ok": True,
                "files": _search(
                    user["id"],
                    query,
                    kind,
                ),
            })

        except ValueError as e:
            return _error(e, 401)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/folders", methods=["POST"])
    def api_create_folder():
        try:
            ensure_db()
            user = _current_user()
            body = request.get_json(silent=True) or {}

            parent_id = body.get("parent_id")
            parent_id = (
                int(parent_id)
                if parent_id not in (None, "", 0, "0")
                else None
            )

            folder = _create_folder(
                user["id"],
                body.get("name"),
                parent_id,
            )

            return jsonify({
                "ok": True,
                "folder": folder,
            })

        except ValueError as e:
            return _error(e, 400)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/folders/rename", methods=["POST"])
    def api_rename_folder():
        try:
            ensure_db()
            user = _current_user()
            body = request.get_json(silent=True) or {}

            _rename_folder(
                user["id"],
                int(body["folder_id"]),
                body.get("name"),
            )

            return jsonify({"ok": True})

        except (ValueError, KeyError) as e:
            return _error(e, 400)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/folders/delete", methods=["POST"])
    def api_delete_folder():
        try:
            ensure_db()
            user = _current_user()
            body = request.get_json(silent=True) or {}

            parent_id = _delete_folder(
                user["id"],
                int(body["folder_id"]),
            )

            return jsonify({
                "ok": True,
                "parent_id": parent_id,
            })

        except (ValueError, KeyError) as e:
            return _error(e, 400)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/files/move", methods=["POST"])
    def api_move_files():
        try:
            ensure_db()
            user = _current_user()
            body = request.get_json(silent=True) or {}

            folder_id = body.get("folder_id")
            folder_id = (
                int(folder_id)
                if folder_id not in (None, "", 0, "0")
                else None
            )

            moved = _move_files(
                user["id"],
                body.get("file_ids") or [],
                folder_id,
            )

            return jsonify({
                "ok": True,
                "moved": moved,
            })

        except ValueError as e:
            return _error(e, 400)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/pending/save", methods=["POST"])
    def api_save_pending():
        try:
            ensure_db()
            user = _current_user()
            body = request.get_json(silent=True) or {}

            ids = [
                int(x)
                for x in (body.get("file_ids") or [])
            ]

            folder_id = body.get("folder_id")
            folder_id = (
                int(folder_id)
                if folder_id not in (None, "", 0, "0")
                else None
            )

            saved = []
            failed = []

            for file_id in ids:
                try:
                    if save_one(
                        user["id"],
                        file_id,
                        folder_id,
                    ):
                        saved.append(file_id)

                except Exception as e:
                    failed.append({
                        "id": file_id,
                        "error": str(e),
                    })

            # Always 200 here. Partial failures are data, not an HTTP error.
            return jsonify({
                "ok": True,
                "saved": saved,
                "failed": failed,
            })

        except ValueError as e:
            return _error(e, 400)

        except Exception as e:
            return _error(e, 500)

    @app.route("/warraq/api/files/retrieve", methods=["POST"])
    def api_retrieve_file():
        try:
            ensure_db()
            user = _current_user()
            body = request.get_json(silent=True) or {}

            retrieve_saved_file_to_user(
                user["id"],
                int(body["file_id"]),
            )

            return jsonify({"ok": True})

        except (ValueError, KeyError) as e:
            return _error(e, 400)

        except Exception as e:
            return _error(e, 500)


    @app.route("/warraq/api/files/delete", methods=["POST"])
    def api_delete_file():
        try:
            ensure_db()
            user = _current_user()
            body = request.get_json(silent=True) or {}

            _delete_saved_file(
                user["id"],
                int(body["file_id"]),
            )

            return jsonify({"ok": True})

        except (ValueError, KeyError) as e:
            return _error(e, 400)

        except Exception as e:
            return _error(e, 500)
