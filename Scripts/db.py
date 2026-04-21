"""Camada SQLite compartilhada (storage/auth) com migração inicial de JSON."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime

import config

_LOCK = threading.Lock()
_INITIALIZED = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.APP_DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    global _INITIALIZED
    with _LOCK:
        if _INITIALIZED:
            return
        os.makedirs(os.path.dirname(config.APP_DB_FILE), exist_ok=True)
        with _connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT 'Novo Chat',
                    url TEXT NOT NULL DEFAULT '',
                    origin_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    idx INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    UNIQUE(chat_id, idx),
                    FOREIGN KEY(chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password TEXT NOT NULL,
                    avatar TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(username) REFERENCES users(username) ON DELETE CASCADE
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, idx)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_exp ON sessions(expires_at)")
            conn.commit()
        _migrate_json_if_needed()
        _INITIALIZED = True


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def _migrate_json_if_needed() -> None:
    with _connect() as conn:
        chats_empty = _table_count(conn, "chats") == 0
        users_empty = _table_count(conn, "users") == 0

        if chats_empty and os.path.exists(config.CHATS_FILE):
            try:
                with open(config.CHATS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            now = datetime.now().isoformat()
            for chat_id, chat in (data or {}).items():
                created = chat.get("created_at") or now
                updated = chat.get("updated_at") or created
                conn.execute(
                    "INSERT OR IGNORE INTO chats(chat_id,title,url,origin_url,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (
                        chat_id,
                        chat.get("title") or "Novo Chat",
                        chat.get("url") or "",
                        chat.get("origin_url") or "",
                        created,
                        updated,
                    ),
                )
                for i, msg in enumerate(chat.get("messages") or []):
                    conn.execute(
                        "INSERT OR REPLACE INTO messages(chat_id,idx,role,content) VALUES(?,?,?,?)",
                        (chat_id, i, msg.get("role") or "assistant", msg.get("content") or ""),
                    )

        if users_empty and os.path.exists(config.USERS_FILE):
            try:
                with open(config.USERS_FILE, "r", encoding="utf-8") as f:
                    users = json.load(f)
            except Exception:
                users = {}
            for username, user in (users or {}).items():
                conn.execute(
                    "INSERT OR IGNORE INTO users(username,password,avatar) VALUES(?,?,?)",
                    (username, user.get("password") or "", user.get("avatar")),
                )

        conn.commit()
