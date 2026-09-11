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
)


def _verify_init_data(init_data: str, max_age=86400):
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

    auth_date = int(pairs.get("auth_date", "0") or 0)
    if not auth_date or abs(int(time.time()) - auth_date) > max_age:
        raise ValueError("Expired Telegram session")

    user = json.loads(pairs.get("user") or "{}")
    if not user.get("id"):
        raise ValueError("Missing Telegram user")

    return user


def _current_user():
    return _verify_init_data(
        request.headers.get("X-Telegram-Init-Data", "")
    )


def _json_error(message, status=400):
    return jsonify({"ok": False, "error": str(message)}), status


def _folder_path_map(conn, user_id):
    rows = conn.execute("""
        WITH RECURSIVE paths AS (
            SELECT id,name,parent_id,name::text AS path
            FROM folders
            WHERE user_id=%s AND parent_id IS NULL

            UNION ALL

            SELECT f.id,f.name,f.parent_id,(p.path || ' / ' || f.name)::text
            FROM folders f
            JOIN paths p ON f.parent_id=p.id
            WHERE f.user_id=%s
        )
        SELECT id,path FROM paths
    """, (user_id, user_id)).fetchall()
    return {r["id"]: r["path"] for r in rows}


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
    if name.endswith((".txt",".md",".csv",".json")) or mime.startswith("text/"):
        return "text"
    if name.endswith((".zip",".rar",".7z",".tar",".gz")):
        return "archive"
    return "document"


def _serialize(row, paths=None):
    fid = row.get("folder_id")
    return {
        "id": row["id"],
        "name": row.get("file_name") or "Telegram file",
        "size": int(row.get("file_size") or 0),
        "size_human": human_size(row.get("file_size") or 0),
        "mime_type": row.get("mime_type"),
        "media_type": row.get("media_type"),
        "kind": _kind(row),
        "folder_id": fid,
        "path": (paths or {}).get(fid, "ملفاتي" if fid is None else ""),
        "status": row.get("status"),
    }


def _bootstrap(user_id):
    def work(conn):
        paths = _folder_path_map(conn, user_id)
        roots = conn.execute("""
            SELECT id,name,icon,parent_id
            FROM folders
            WHERE user_id=%s AND parent_id IS NULL
            ORDER BY id
        """, (user_id,)).fetchall()
        inbox = conn.execute("""
            SELECT COUNT(*) AS n FROM files
            WHERE user_id=%s AND status='pending'
        """, (user_id,)).fetchone()["n"]
        total = conn.execute("""
            SELECT COUNT(*) AS n FROM files
            WHERE user_id=%s AND status='saved'
        """, (user_id,)).fetchone()["n"]
        recent = conn.execute("""
            SELECT id,file_name,file_size,mime_type,media_type,folder_id,status
            FROM files
            WHERE user_id=%s AND status='saved'
            ORDER BY COALESCE(saved_at,created_at) DESC,id DESC
            LIMIT 10
        """, (user_id,)).fetchall()
        return {
            "folders":[dict(r) for r in roots],
            "inbox_count":inbox,
            "total_files":total,
            "recent":[_serialize(r,paths) for r in recent],
        }
    return run_tx(work)


def _folder(user_id, folder_id):
    def work(conn):
        paths = _folder_path_map(conn,user_id)
        if folder_id is None:
            children = conn.execute("""
                SELECT id,name,icon,parent_id FROM folders
                WHERE user_id=%s AND parent_id IS NULL
                ORDER BY lower(name),id
            """,(user_id,)).fetchall()
            files = conn.execute("""
                SELECT id,file_name,file_size,mime_type,media_type,folder_id,status
                FROM files
                WHERE user_id=%s AND status='saved' AND folder_id IS NULL
                ORDER BY id DESC
            """,(user_id,)).fetchall()
            breadcrumb=[]
        else:
            exists = conn.execute("""
                SELECT id FROM folders WHERE user_id=%s AND id=%s
            """,(user_id,folder_id)).fetchone()
            if not exists:
                raise ValueError("المجلد غير موجود")
            children = conn.execute("""
                SELECT id,name,icon,parent_id FROM folders
                WHERE user_id=%s AND parent_id=%s
                ORDER BY lower(name),id
            """,(user_id,folder_id)).fetchall()
            files = conn.execute("""
                SELECT id,file_name,file_size,mime_type,media_type,folder_id,status
                FROM files
                WHERE user_id=%s AND status='saved' AND folder_id=%s
                ORDER BY id DESC
            """,(user_id,folder_id)).fetchall()
            breadcrumb = conn.execute("""
                WITH RECURSIVE tree AS (
                    SELECT id,name,parent_id,1 depth
                    FROM folders WHERE user_id=%s AND id=%s
                    UNION ALL
                    SELECT f.id,f.name,f.parent_id,t.depth+1
                    FROM folders f JOIN tree t ON f.id=t.parent_id
                    WHERE f.user_id=%s
                )
                SELECT id,name,parent_id FROM tree ORDER BY depth DESC
            """,(user_id,folder_id,user_id)).fetchall()
        return {
            "children":[dict(r) for r in children],
            "files":[_serialize(r,paths) for r in files],
            "breadcrumb":[dict(r) for r in breadcrumb],
        }
    return run_tx(work)


def _inbox(user_id):
    def work(conn):
        rows=conn.execute("""
            SELECT id,file_name,file_size,mime_type,media_type,folder_id,status
            FROM files WHERE user_id=%s AND status='pending'
            ORDER BY id DESC
        """,(user_id,)).fetchall()
        return [_serialize(r) for r in rows]
    return run_tx(work)


def _search(user_id,q,kind):
    def work(conn):
        paths=_folder_path_map(conn,user_id)
        rows=conn.execute("""
            SELECT id,file_name,file_size,mime_type,media_type,folder_id,status
            FROM files
            WHERE user_id=%s AND status='saved'
              AND (%s='' OR file_name ILIKE %s)
            ORDER BY id DESC LIMIT 100
        """,(user_id,q,f"%{q}%")).fetchall()
        out=[_serialize(r,paths) for r in rows]
        if kind and kind!="all":
            out=[r for r in out if r["kind"]==kind]
        return out
    return run_tx(work)


def _create_folder(user_id,name,parent_id):
    name=" ".join((name or "").strip().split())
    if not name:
        raise ValueError("اكتب اسم المجلد")
    if "/" in name or len(name)>60:
        raise ValueError("اسم المجلد غير صالح")

    def work(conn):
        if parent_id is not None:
            if not conn.execute(
                "SELECT 1 FROM folders WHERE user_id=%s AND id=%s",
                (user_id,parent_id)
            ).fetchone():
                raise ValueError("المجلد الأب غير موجود")

        if parent_id is None:
            exists=conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s AND parent_id IS NULL
                  AND lower(name)=lower(%s)
            """,(user_id,name)).fetchone()
        else:
            exists=conn.execute("""
                SELECT 1 FROM folders
                WHERE user_id=%s AND parent_id=%s
                  AND lower(name)=lower(%s)
            """,(user_id,parent_id,name)).fetchone()
        if exists:
            raise ValueError("يوجد مجلد بهذا الاسم هنا")

        row=conn.execute("""
            INSERT INTO folders(user_id,name,icon,parent_id)
            VALUES(%s,%s,'📁',%s)
            RETURNING id,name,icon,parent_id
        """,(user_id,name,parent_id)).fetchone()
        return dict(row)
    return run_tx(work)


def _move(user_id,file_ids,folder_id):
    ids=[int(x) for x in file_ids]
    if not ids:
        raise ValueError("لم تحدد ملفات")

    def work(conn):
        if folder_id is not None and not conn.execute(
            "SELECT 1 FROM folders WHERE user_id=%s AND id=%s",
            (user_id,folder_id)
        ).fetchone():
            raise ValueError("المجلد غير موجود")

        rows=conn.execute("""
            UPDATE files SET folder_id=%s
            WHERE user_id=%s AND status='saved' AND id=ANY(%s)
            RETURNING id
        """,(folder_id,user_id,ids)).fetchall()
        return len(rows)
    return run_tx(work)


def register_warraq_miniapp(app):
    @app.route("/warraq")
    def warraq_page():
        return send_file(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),"warraq.html")
        )

    @app.route("/warraq/api/bootstrap")
    def api_bootstrap():
        try:
            ensure_db(); user=_current_user(); ensure_user(user["id"])
            return jsonify({"ok":True,"user":user,**_bootstrap(user["id"])})
        except ValueError as e:
            return _json_error(e,401)
        except Exception as e:
            return _json_error(e,500)

    @app.route("/warraq/api/folder")
    def api_folder():
        try:
            ensure_db(); user=_current_user()
            raw=request.args.get("id")
            folder_id=int(raw) if raw not in (None,"","root","0") else None
            return jsonify({"ok":True,**_folder(user["id"],folder_id)})
        except ValueError as e:
            return _json_error(e,400)
        except Exception as e:
            return _json_error(e,500)

    @app.route("/warraq/api/inbox")
    def api_inbox():
        try:
            ensure_db(); user=_current_user()
            return jsonify({"ok":True,"files":_inbox(user["id"])})
        except ValueError as e:
            return _json_error(e,401)
        except Exception as e:
            return _json_error(e,500)

    @app.route("/warraq/api/search")
    def api_search():
        try:
            ensure_db(); user=_current_user()
            q=(request.args.get("q") or "").strip()
            kind=(request.args.get("kind") or "all").strip()
            return jsonify({"ok":True,"files":_search(user["id"],q,kind)})
        except ValueError as e:
            return _json_error(e,401)
        except Exception as e:
            return _json_error(e,500)

    @app.route("/warraq/api/folders",methods=["POST"])
    def api_new_folder():
        try:
            ensure_db(); user=_current_user()
            body=request.get_json(silent=True) or {}
            pid=body.get("parent_id")
            pid=int(pid) if pid not in (None,"",0,"0") else None
            return jsonify({"ok":True,"folder":_create_folder(user["id"],body.get("name"),pid)})
        except ValueError as e:
            return _json_error(e,400)
        except Exception as e:
            return _json_error(e,500)

    @app.route("/warraq/api/files/move",methods=["POST"])
    def api_move():
        try:
            ensure_db(); user=_current_user()
            body=request.get_json(silent=True) or {}
            pid=body.get("folder_id")
            pid=int(pid) if pid not in (None,"",0,"0") else None
            return jsonify({"ok":True,"moved":_move(user["id"],body.get("file_ids") or [],pid)})
        except ValueError as e:
            return _json_error(e,400)
        except Exception as e:
            return _json_error(e,500)

    @app.route("/warraq/api/pending/save",methods=["POST"])
    def api_save_pending():
        try:
            ensure_db(); user=_current_user()
            body=request.get_json(silent=True) or {}
            ids=[int(x) for x in (body.get("file_ids") or [])]
            pid=body.get("folder_id")
            pid=int(pid) if pid not in (None,"",0,"0") else None
            saved=[]; failed=[]
            for file_id in ids:
                try:
                    if save_one(user["id"],file_id,pid):
                        saved.append(file_id)
                except Exception as e:
                    failed.append({"id":file_id,"error":str(e)})
            return jsonify({"ok":len(failed)==0,"saved":saved,"failed":failed})
        except ValueError as e:
            return _json_error(e,400)
        except Exception as e:
            return _json_error(e,500)

    @app.route("/warraq/api/files/retrieve",methods=["POST"])
    def api_retrieve():
        try:
            ensure_db(); user=_current_user()
            body=request.get_json(silent=True) or {}
            row=get_file(user["id"],int(body["file_id"]),saved_only=True)
            if not row or not row.get("storage_message_id"):
                raise ValueError("الملف غير موجود في المخزن")
            copy_message(user["id"],STORAGE_CHAT_ID,row["storage_message_id"])
            return jsonify({"ok":True})
        except (ValueError,KeyError) as e:
            return _json_error(e,400)
        except Exception as e:
            return _json_error(e,500)
