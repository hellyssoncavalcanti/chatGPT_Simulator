import copy
import hashlib
import threading
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from utils import log
import config
import db

_lock = threading.Lock()


def _normalize_lookup_value(value):
    if value is None:
        return None
    value = str(value).strip()
    if not value or value.lower() in {"null", "none", "undefined"}:
        return None
    return value


def _extract_origin_lookup_ids(origin_url: str):
    if not origin_url:
        return {"id_paciente": None, "id_atendimento": None, "id_receita": None}
    try:
        query = parse_qs(urlparse(origin_url).query, keep_blank_values=True)
    except Exception:
        query = {}
    return {
        "id_paciente": _normalize_lookup_value((query.get("id_paciente") or [None])[0]),
        "id_atendimento": _normalize_lookup_value((query.get("id_atendimento") or [None])[0]),
        "id_receita": _normalize_lookup_value((query.get("id_receita") or [None])[0]),
    }


def get_meta(content):
    if not content:
        return ""
    return hashlib.md5(content.encode("utf-8", errors="ignore")).hexdigest()


def _chat_dict(conn, chat_id: str):
    chat = conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if not chat:
        return None
    msgs = conn.execute("SELECT role,content FROM messages WHERE chat_id=? ORDER BY idx", (chat_id,)).fetchall()
    return {
        "title": chat["title"],
        "url": chat["url"],
        "chromium_profile": chat["chromium_profile"] or "",
        "origin_url": chat["origin_url"],
        "created_at": chat["created_at"],
        "updated_at": chat["updated_at"],
        "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
    }


def _ensure_chat(conn, chat_id, title="Novo Chat", url="", origin_url="", chromium_profile=""):
    row = conn.execute("SELECT chat_id FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
    if row:
        conn.execute(
            "UPDATE chats SET "
            "title=COALESCE(NULLIF(title,''),?), "
            "url=COALESCE(NULLIF(url,''),?), "
            "chromium_profile=COALESCE(NULLIF(chromium_profile,''),?), "
            "origin_url=COALESCE(NULLIF(origin_url,''),?) "
            "WHERE chat_id=?",
            (title or "Novo Chat", url or "", chromium_profile or "", origin_url or "", chat_id),
        )
        return
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO chats(chat_id,title,url,chromium_profile,origin_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (chat_id, title or "Novo Chat", url or "", chromium_profile or "", origin_url or "", now, now),
    )


def load_chats():
    db.init_db()
    with _lock, db._connect() as conn:
        rows = conn.execute("SELECT chat_id FROM chats ORDER BY COALESCE(updated_at,created_at) ASC").fetchall()
        result = {}
        for r in rows:
            result[r["chat_id"]] = _chat_dict(conn, r["chat_id"])
        return copy.deepcopy(result)


def append_message(chat_id, role, content):
    db.init_db()
    with _lock, db._connect() as conn:
        _ensure_chat(conn, chat_id)
        last = conn.execute(
            "SELECT role,content FROM messages WHERE chat_id=? ORDER BY idx DESC LIMIT 1", (chat_id,)
        ).fetchone()
        if not last or last["role"] != role or last["content"] != content:
            idx = conn.execute("SELECT COALESCE(MAX(idx),-1)+1 FROM messages WHERE chat_id=?", (chat_id,)).fetchone()[0]
            conn.execute(
                "INSERT INTO messages(chat_id,idx,role,content) VALUES(?,?,?,?)", (chat_id, int(idx), role, content)
            )
        conn.execute("UPDATE chats SET updated_at=? WHERE chat_id=?", (datetime.now().isoformat(), chat_id))
        conn.commit()


def save_chat(chat_id, title, url, messages, origin_url=None, chromium_profile=None):
    db.init_db()
    with _lock, db._connect() as conn:
        _ensure_chat(
            conn,
            chat_id,
            title=title or "Novo Chat",
            url=url or "",
            origin_url=origin_url or "",
            chromium_profile=chromium_profile or "",
        )
        chat = _chat_dict(conn, chat_id)
        existing = {(m.get("role"), m.get("content")) for m in chat["messages"]}
        idx = len(chat["messages"])
        for msg in messages:
            pair = (msg.get("role"), msg.get("content"))
            if pair in existing:
                continue
            conn.execute(
                "INSERT INTO messages(chat_id,idx,role,content) VALUES(?,?,?,?)",
                (chat_id, idx, msg.get("role") or "assistant", msg.get("content") or ""),
            )
            existing.add(pair)
            idx += 1
        conn.execute(
            "UPDATE chats SET title=?, url=?, chromium_profile=?, origin_url=?, updated_at=? WHERE chat_id=?",
            (
                title or chat["title"],
                url or chat["url"],
                chromium_profile or chat.get("chromium_profile", ""),
                origin_url or chat["origin_url"],
                datetime.now().isoformat(),
                chat_id,
            ),
        )
        conn.commit()
        return _chat_dict(conn, chat_id)


def update_full_history(chat_id, browser_messages, title=None, url=None, chromium_profile=None):
    db.init_db()
    with _lock, db._connect() as conn:
        _ensure_chat(conn, chat_id, title=title or "Novo Chat", url=url or "", chromium_profile=chromium_profile or "")
        local = _chat_dict(conn, chat_id)
        local_msgs = local["messages"]
        has_changes = False
        for i, b_msg in enumerate(browser_messages):
            b_content = b_msg.get("content") or ""
            if i < len(local_msgs):
                l_content = local_msgs[i].get("content") or ""
                if get_meta(b_content) != get_meta(l_content) and (len(b_content) > len(l_content) or b_content != l_content):
                    conn.execute(
                        "UPDATE messages SET role=?, content=? WHERE chat_id=? AND idx=?",
                        (b_msg.get("role") or "assistant", b_content, chat_id, i),
                    )
                    has_changes = True
            else:
                conn.execute(
                    "INSERT INTO messages(chat_id,idx,role,content) VALUES(?,?,?,?)",
                    (chat_id, i, b_msg.get("role") or "assistant", b_content),
                )
                has_changes = True
        if len(local_msgs) > len(browser_messages):
            conn.execute("DELETE FROM messages WHERE chat_id=? AND idx>=?", (chat_id, len(browser_messages)))
            has_changes = True
        if title and title != local["title"]:
            conn.execute("UPDATE chats SET title=? WHERE chat_id=?", (title, chat_id))
            has_changes = True
        if url and url != local["url"]:
            conn.execute("UPDATE chats SET url=? WHERE chat_id=?", (url, chat_id))
            has_changes = True
        if chromium_profile and chromium_profile != (local.get("chromium_profile") or ""):
            conn.execute("UPDATE chats SET chromium_profile=? WHERE chat_id=?", (chromium_profile, chat_id))
            has_changes = True
        if has_changes:
            conn.execute("UPDATE chats SET updated_at=? WHERE chat_id=?", (datetime.now().isoformat(), chat_id))
            conn.commit()
            log("storage.py", "Histórico sincronizado com sucesso.")
        return has_changes


def find_chat_by_origin(origin_url: str):
    if not origin_url:
        return None
    target_ids = _extract_origin_lookup_ids(origin_url)
    has_target_ids = any(v is not None for v in target_ids.values())
    data = load_chats()
    candidates = []
    for chat_id, chat in data.items():
        chat_origin_url = chat.get("origin_url") or ""
        chat_ids = _extract_origin_lookup_ids(chat_origin_url)
        if has_target_ids:
            if chat_ids != target_ids:
                continue
        elif chat_origin_url != origin_url:
            continue
        candidates.append((chat.get("updated_at") or chat.get("created_at") or "", chat_id, chat))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _dt, chat_id, chat = candidates[0]
    return {
        "chat_id": chat_id,
        "title": chat.get("title") or "Novo Chat",
        "url": chat.get("url") or "",
        "origin_url": chat.get("origin_url") or "",
        "chromium_profile": chat.get("chromium_profile") or "",
        "messages": chat.get("messages") or [],
        "updated_at": chat.get("updated_at") or chat.get("created_at") or "",
    }


def delete_chat(chat_id: str) -> bool:
    if not chat_id:
        return False
    db.init_db()
    with _lock, db._connect() as conn:
        deleted = conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,)).rowcount
        conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
        conn.commit()
        if deleted:
            log("storage.py", f"Chat {chat_id} removido do histórico local.")
        return bool(deleted)


def delete_chats_by_origin(origin_url: str) -> int:
    if not origin_url:
        return 0
    data = load_chats()
    target_ids = _extract_origin_lookup_ids(origin_url)
    has_target_ids = any(v is not None for v in target_ids.values())
    to_delete = []
    for chat_id, chat in data.items():
        chat_origin_url = chat.get("origin_url") or ""
        chat_ids = _extract_origin_lookup_ids(chat_origin_url)
        if has_target_ids and chat_ids == target_ids:
            to_delete.append(chat_id)
        elif not has_target_ids and chat_origin_url == origin_url:
            to_delete.append(chat_id)
    if not to_delete:
        return 0
    db.init_db()
    with _lock, db._connect() as conn:
        conn.executemany("DELETE FROM messages WHERE chat_id=?", [(cid,) for cid in to_delete])
        conn.executemany("DELETE FROM chats WHERE chat_id=?", [(cid,) for cid in to_delete])
        conn.commit()
    log("storage.py", f"{len(to_delete)} chat(s) removido(s) por origin_url.")
    return len(to_delete)
