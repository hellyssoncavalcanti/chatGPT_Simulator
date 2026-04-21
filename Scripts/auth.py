import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta

import config
import db

SESSION_TTL_HOURS = int(getattr(config, "SESSION_TTL_HOURS", 24))


def hash_password(password):
    return hashlib.sha256((password or "").encode()).hexdigest()


def _ensure_default_admin(conn):
    row = conn.execute("SELECT username FROM users WHERE username='admin'").fetchone()
    if row:
        return
    conn.execute(
        "INSERT INTO users(username,password,avatar) VALUES(?,?,?)",
        ("admin", hash_password("admin"), None),
    )
    conn.commit()


def load_users():
    db.init_db()
    with db._connect() as conn:
        _ensure_default_admin(conn)
        rows = conn.execute("SELECT username,password,avatar FROM users ORDER BY username").fetchall()
        users = {r["username"]: {"password": r["password"], "avatar": r["avatar"]} for r in rows}

    # compatibilidade: espelha em JSON se caminho existir
    try:
        os.makedirs(os.path.dirname(config.USERS_FILE), exist_ok=True)
        with open(config.USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=4, ensure_ascii=False)
    except Exception:
        pass
    return users


def save_users(users_data):
    db.init_db()
    with db._connect() as conn:
        conn.execute("DELETE FROM users")
        for username, user in (users_data or {}).items():
            conn.execute(
                "INSERT INTO users(username,password,avatar) VALUES(?,?,?)",
                (username, user.get("password") or "", user.get("avatar")),
            )
        conn.commit()


def _cleanup_sessions(conn):
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (datetime.utcnow().isoformat(),))


def verify_login(username, password):
    db.init_db()
    with db._connect() as conn:
        _cleanup_sessions(conn)
        row = conn.execute("SELECT password FROM users WHERE username=?", (username,)).fetchone()
        if not row or row["password"] != hash_password(password):
            return None
        token = str(uuid.uuid4())
        now = datetime.utcnow()
        exp = now + timedelta(hours=max(1, SESSION_TTL_HOURS))
        conn.execute(
            "INSERT INTO sessions(token,username,expires_at,created_at) VALUES(?,?,?,?)",
            (token, username, exp.isoformat(), now.isoformat()),
        )
        conn.commit()
        return token


def change_password(username, new_password):
    if not username or not new_password:
        return False
    db.init_db()
    with db._connect() as conn:
        updated = conn.execute(
            "UPDATE users SET password=? WHERE username=?",
            (hash_password(new_password), username),
        ).rowcount
        conn.commit()
        return bool(updated)


def update_avatar(username, filename):
    db.init_db()
    with db._connect() as conn:
        updated = conn.execute("UPDATE users SET avatar=? WHERE username=?", (filename, username)).rowcount
        conn.commit()
        return bool(updated)


def get_user_info(token):
    if not token:
        return None
    db.init_db()
    with db._connect() as conn:
        _cleanup_sessions(conn)
        row = conn.execute(
            "SELECT s.username, u.avatar FROM sessions s JOIN users u ON u.username=s.username WHERE s.token=?",
            (token,),
        ).fetchone()
        conn.commit()
        if not row:
            return None
        return {"username": row["username"], "avatar": row["avatar"]}


def check_session(request):
    token = request.cookies.get("session_token")
    if not token:
        return None
    db.init_db()
    with db._connect() as conn:
        _cleanup_sessions(conn)
        row = conn.execute("SELECT username FROM sessions WHERE token=?", (token,)).fetchone()
        conn.commit()
        return row["username"] if row else None


def logout(token):
    if not token:
        return
    db.init_db()
    with db._connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
