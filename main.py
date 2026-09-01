from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from contextlib import closing
import os
import sqlite3
import time
import uuid

APP_TITLE = "Atherium Chat API"

API_KEY = os.getenv("API_KEY", "atherium-local-key")
DATABASE_PATH = os.getenv("DATABASE_PATH", "atherium_chat.db")

MAX_MESSAGE_LENGTH = 250
MESSAGE_LIFETIME = 2 * 60 * 60

# Presence disappears after this many seconds without heartbeat.
PRESENCE_TTL = 15

# Stronger than username-based detection: configure actual Roblox UserIds.
# Example Render env var:
# DEV_USER_IDS=123456789,987654321
DEV_USER_IDS = {
    int(x.strip())
    for x in os.getenv("DEV_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

app = FastAPI(
    title=APP_TITLE,
    version="1.1.0",
    description="Atherium global chat + presence API"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DATABASE_PATH,
        timeout=10,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def init_database():
    with closing(get_db()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                user_id INTEGER,
                message TEXT NOT NULL,
                timestamp REAL NOT NULL
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
            ON messages(timestamp)
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS presence (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                place_id INTEGER,
                job_id TEXT,
                place_name TEXT,
                x REAL,
                y REAL,
                z REAL,
                health REAL,
                max_health REAL,
                role TEXT NOT NULL DEFAULT 'user',
                last_seen REAL NOT NULL
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_presence_last_seen
            ON presence(last_seen)
        """)

        db.commit()


init_database()


# ============================================================
# MODELS
# ============================================================

class ChatMessage(BaseModel):
    sender: str = Field(..., min_length=1, max_length=32)
    user_id: Optional[int] = None
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


class PresenceUpdate(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    user_id: int
    place_id: Optional[int] = None
    job_id: Optional[str] = Field(default=None, max_length=64)
    place_name: Optional[str] = Field(default=None, max_length=100)
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    health: Optional[float] = None
    max_health: Optional[float] = None


# ============================================================
# HELPERS
# ============================================================

def check_api_key(x_api_key: Optional[str]):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def cleanup_messages():
    cutoff = time.time() - MESSAGE_LIFETIME

    with closing(get_db()) as db:
        db.execute(
            "DELETE FROM messages WHERE timestamp <= ?",
            (cutoff,)
        )
        db.commit()


def cleanup_presence():
    cutoff = time.time() - PRESENCE_TTL

    with closing(get_db()) as db:
        db.execute(
            "DELETE FROM presence WHERE last_seen < ?",
            (cutoff,)
        )
        db.commit()


def row_to_message(row):
    return {
        "id": row["id"],
        "sender": row["sender"],
        "user_id": row["user_id"],
        "message": row["message"],
        "timestamp": row["timestamp"],
    }


def row_to_presence(row):
    return {
        "user_id": row["user_id"],
        "username": row["username"],
        "place_id": row["place_id"],
        "job_id": row["job_id"],
        "place_name": row["place_name"] or "Unknown Place",
        "x": row["x"],
        "y": row["y"],
        "z": row["z"],
        "health": row["health"],
        "max_health": row["max_health"],
        "role": row["role"],
        "last_seen": row["last_seen"],
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
def root():
    cleanup_messages()
    cleanup_presence()

    with closing(get_db()) as db:
        message_count = db.execute(
            "SELECT COUNT(*) AS count FROM messages"
        ).fetchone()

        presence_count = db.execute(
            "SELECT COUNT(*) AS count FROM presence"
        ).fetchone()

    return {
        "status": "ok",
        "service": APP_TITLE,
        "messages": int(message_count["count"]),
        "online": int(presence_count["count"]),
        "message_lifetime_seconds": MESSAGE_LIFETIME,
        "presence_ttl_seconds": PRESENCE_TTL,
    }


# ============================================================
# CHAT
# ============================================================

@app.get("/chat")
def get_chat(
    after: float = Query(default=0, ge=0),
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)
    cleanup_messages()

    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT id, sender, user_id, message, timestamp
            FROM messages
            WHERE timestamp > ?
            ORDER BY timestamp ASC
        """, (after,)).fetchall()

    return {
        "success": True,
        "messages": [row_to_message(row) for row in rows]
    }


@app.post("/chat")
def send_chat(
    data: ChatMessage,
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)

    sender = data.sender.strip()
    message = data.message.strip()

    if not sender:
        raise HTTPException(status_code=400, detail="Empty sender")

    if not message:
        raise HTTPException(status_code=400, detail="Empty message")

    now = time.time()

    entry = {
        "id": uuid.uuid4().hex,
        "sender": sender[:32],
        "user_id": data.user_id,
        "message": message,
        "timestamp": now,
    }

    with closing(get_db()) as db:
        db.execute("""
            INSERT INTO messages (
                id, sender, user_id, message, timestamp
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            entry["id"],
            entry["sender"],
            entry["user_id"],
            entry["message"],
            entry["timestamp"],
        ))
        db.commit()

    return {
        "success": True,
        "message": entry
    }


@app.delete("/chat")
def clear_chat(
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)

    with closing(get_db()) as db:
        db.execute("DELETE FROM messages")
        db.commit()

    return {"success": True}


# ============================================================
# PRESENCE
# ============================================================

@app.post("/presence")
def update_presence(
    data: PresenceUpdate,
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)

    username = data.username.strip()

    if not username:
        raise HTTPException(status_code=400, detail="Empty username")

    role = "dev" if data.user_id in DEV_USER_IDS else "user"
    now = time.time()

    with closing(get_db()) as db:
        db.execute("""
            INSERT INTO presence (
                user_id,
                username,
                place_id,
                job_id,
                place_name,
                x,
                y,
                z,
                health,
                max_health,
                role,
                last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                place_id = excluded.place_id,
                job_id = excluded.job_id,
                place_name = excluded.place_name,
                x = excluded.x,
                y = excluded.y,
                z = excluded.z,
                health = excluded.health,
                max_health = excluded.max_health,
                role = excluded.role,
                last_seen = excluded.last_seen
        """, (
            data.user_id,
            username[:32],
            data.place_id,
            data.job_id,
            (data.place_name or "Unknown Place")[:100],
            data.x,
            data.y,
            data.z,
            data.health,
            data.max_health,
            role,
            now,
        ))
        db.commit()

    return {
        "success": True,
        "role": role,
        "last_seen": now,
    }


@app.get("/presence")
def get_presence(
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)
    cleanup_presence()

    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT
                user_id,
                username,
                place_id,
                job_id,
                place_name,
                x,
                y,
                z,
                health,
                max_health,
                role,
                last_seen
            FROM presence
            WHERE last_seen >= ?
            ORDER BY
                CASE WHEN role = 'dev' THEN 0 ELSE 1 END,
                username COLLATE NOCASE ASC
        """, (time.time() - PRESENCE_TTL,)).fetchall()

    return {
        "success": True,
        "online": [row_to_presence(row) for row in rows]
    }


@app.delete("/presence/{user_id}")
def remove_presence(
    user_id: int,
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)

    with closing(get_db()) as db:
        db.execute(
            "DELETE FROM presence WHERE user_id = ?",
            (user_id,)
        )
        db.commit()

    return {"success": True}


# ============================================================
# STATUS
# ============================================================

@app.get("/status")
def status(
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)
    cleanup_messages()
    cleanup_presence()

    with closing(get_db()) as db:
        message_count = db.execute(
            "SELECT COUNT(*) AS count FROM messages"
        ).fetchone()

        presence_count = db.execute(
            "SELECT COUNT(*) AS count FROM presence"
        ).fetchone()

    return {
        "online": True,
        "messages": int(message_count["count"]),
        "users_online": int(presence_count["count"]),
        "message_lifetime_seconds": MESSAGE_LIFETIME,
        "presence_ttl_seconds": PRESENCE_TTL,
    }


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        reload=False,
    )
