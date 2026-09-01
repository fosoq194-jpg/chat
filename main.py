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
PRESENCE_TTL = 15

DEV_USER_IDS = {
    int(x.strip())
    for x in os.getenv("DEV_USER_IDS", "").split(",")
    if x.strip().isdigit()
}
DEV_USERNAMES = {
    x.strip().lower()
    for x in os.getenv("DEV_USERNAMES", "zovetoya").split(",")
    if x.strip()
}

app = FastAPI(
    title=APP_TITLE,
    version="1.2.0",
    description="Atherium global chat + presence API",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=10, check_same_thread=False)
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
                role TEXT NOT NULL DEFAULT 'user',
                timestamp REAL NOT NULL
            )
        """)
        cols = {row["name"] for row in db.execute("PRAGMA table_info(messages)").fetchall()}
        if "role" not in cols:
            db.execute("ALTER TABLE messages ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)")

        db.execute("""
            CREATE TABLE IF NOT EXISTS presence (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                place_id INTEGER,
                job_id TEXT,
                place_name TEXT,
                device TEXT,
                x REAL, y REAL, z REAL,
                health REAL,
                max_health REAL,
                role TEXT NOT NULL DEFAULT 'user',
                last_seen REAL NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_presence_last_seen ON presence(last_seen)")
        db.commit()


init_database()


class ChatMessage(BaseModel):
    sender: str = Field(..., min_length=1, max_length=32)
    user_id: Optional[int] = None
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


class PresenceUpdate(BaseModel):
    session_id: str = Field(..., min_length=8, max_length=64)
    username: str = Field(..., min_length=1, max_length=32)
    user_id: int
    place_id: Optional[int] = None
    job_id: Optional[str] = Field(default=None, max_length=64)
    place_name: Optional[str] = Field(default=None, max_length=100)
    device: Optional[str] = Field(default=None, max_length=16)
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    health: Optional[float] = None
    max_health: Optional[float] = None


def check_api_key(x_api_key: Optional[str]):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def is_developer(user_id: Optional[int], username: str) -> bool:
    return (user_id is not None and user_id in DEV_USER_IDS) or username.strip().lower() in DEV_USERNAMES


def cleanup_messages():
    cutoff = time.time() - MESSAGE_LIFETIME
    with closing(get_db()) as db:
        db.execute("DELETE FROM messages WHERE timestamp <= ?", (cutoff,))
        db.commit()


def cleanup_presence():
    cutoff = time.time() - PRESENCE_TTL
    with closing(get_db()) as db:
        db.execute("DELETE FROM presence WHERE last_seen < ?", (cutoff,))
        db.commit()


def message_dict(row):
    return {
        "id": row["id"],
        "sender": row["sender"],
        "user_id": row["user_id"],
        "message": row["message"],
        "role": row["role"],
        "timestamp": row["timestamp"],
    }


def presence_dict(row):
    return {
        "session_id": row["session_id"],
        "user_id": row["user_id"],
        "username": row["username"],
        "place_id": row["place_id"],
        "job_id": row["job_id"],
        "place_name": row["place_name"] or "Unknown Place",
        "device": row["device"] or "Desktop",
        "x": row["x"],
        "y": row["y"],
        "z": row["z"],
        "health": row["health"],
        "max_health": row["max_health"],
        "role": row["role"],
        "last_seen": row["last_seen"],
    }


@app.get("/")
def root():
    cleanup_messages()
    cleanup_presence()
    with closing(get_db()) as db:
        messages = db.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
        online = db.execute("SELECT COUNT(*) AS count FROM presence").fetchone()["count"]
    return {
        "status": "ok",
        "service": APP_TITLE,
        "messages": int(messages),
        "online": int(online),
        "message_lifetime_seconds": MESSAGE_LIFETIME,
        "presence_ttl_seconds": PRESENCE_TTL,
    }


@app.get("/chat")
def get_chat(after: float = Query(default=0, ge=0), x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    cleanup_messages()
    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT id, sender, user_id, message, role, timestamp
            FROM messages
            WHERE timestamp > ?
            ORDER BY timestamp ASC
        """, (after,)).fetchall()
    return {"success": True, "messages": [message_dict(row) for row in rows]}


@app.post("/chat")
def send_chat(data: ChatMessage, x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    sender = data.sender.strip()
    message = data.message.strip()
    if not sender:
        raise HTTPException(status_code=400, detail="Empty sender")
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")

    now = time.time()
    role = "dev" if is_developer(data.user_id, sender) else "user"
    entry = {
        "id": uuid.uuid4().hex,
        "sender": sender[:32],
        "user_id": data.user_id,
        "message": message,
        "role": role,
        "timestamp": now,
    }
    with closing(get_db()) as db:
        db.execute("""
            INSERT INTO messages (id, sender, user_id, message, role, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            entry["id"], entry["sender"], entry["user_id"], entry["message"],
            entry["role"], entry["timestamp"]
        ))
        db.commit()
    return {"success": True, "message": entry}


@app.delete("/chat")
def clear_chat(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    with closing(get_db()) as db:
        db.execute("DELETE FROM messages")
        db.commit()
    return {"success": True}


@app.post("/presence")
def update_presence(data: PresenceUpdate, x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    username = data.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="Empty username")

    now = time.time()
    role = "dev" if is_developer(data.user_id, username) else "user"
    with closing(get_db()) as db:
        db.execute("""
            INSERT INTO presence (
                session_id, user_id, username, place_id, job_id, place_name, device,
                x, y, z, health, max_health, role, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                user_id=excluded.user_id, username=excluded.username, place_id=excluded.place_id,
                job_id=excluded.job_id, place_name=excluded.place_name, device=excluded.device,
                x=excluded.x, y=excluded.y, z=excluded.z, health=excluded.health,
                max_health=excluded.max_health, role=excluded.role, last_seen=excluded.last_seen
        """, (
            data.session_id, data.user_id, username[:32], data.place_id, data.job_id,
            (data.place_name or "Unknown Place")[:100], (data.device or "Desktop")[:16],
            data.x, data.y, data.z, data.health, data.max_health, role, now
        ))
        db.commit()
    return {"success": True, "session_id": data.session_id, "role": role, "last_seen": now}


@app.get("/presence")
def get_presence(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    cleanup_presence()
    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT session_id, user_id, username, place_id, job_id, place_name, device,
                   x, y, z, health, max_health, role, last_seen
            FROM presence
            WHERE last_seen >= ?
            ORDER BY CASE WHEN role='dev' THEN 0 ELSE 1 END, username COLLATE NOCASE ASC, session_id ASC
        """, (time.time() - PRESENCE_TTL,)).fetchall()
    return {"success": True, "online": [presence_dict(row) for row in rows]}


@app.delete("/presence/{session_id}")
def remove_presence(session_id: str, x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    with closing(get_db()) as db:
        db.execute("DELETE FROM presence WHERE session_id = ?", (session_id,))
        db.commit()
    return {"success": True}


@app.get("/status")
def status(x_api_key: Optional[str] = Header(default=None)):
    check_api_key(x_api_key)
    cleanup_messages()
    cleanup_presence()
    with closing(get_db()) as db:
        messages = db.execute("SELECT COUNT(*) AS count FROM messages").fetchone()["count"]
        online = db.execute("SELECT COUNT(*) AS count FROM presence").fetchone()["count"]
    return {
        "online": True,
        "messages": int(messages),
        "users_online": int(online),
        "message_lifetime_seconds": MESSAGE_LIFETIME,
        "presence_ttl_seconds": PRESENCE_TTL,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
