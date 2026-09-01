from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
import time
import uuid
import threading
import uvicorn

app = FastAPI(title="Atherium Chat API")

MESSAGE_LIFETIME = 2 * 60 * 60
MAX_MESSAGE_LENGTH = 250
API_KEY = "atherium-local-key"

messages = []
messages_lock = threading.Lock()


class ChatMessage(BaseModel):
    sender: str
    user_id: Optional[int] = None
    message: str


def cleanup_messages():
    now = time.time()

    with messages_lock:
        messages[:] = [
            msg
            for msg in messages
            if now - msg["timestamp"] <= MESSAGE_LIFETIME
        ]


def check_api_key(x_api_key: Optional[str]):
    if x_api_key != API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key"
        )


@app.get("/")
def root():
    cleanup_messages()

    return {
        "status": "ok",
        "service": "Atherium Chat",
        "messages": len(messages)
    }


@app.get("/chat")
def get_chat(
    after: float = 0,
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)
    cleanup_messages()

    with messages_lock:
        result = [
            msg
            for msg in messages
            if msg["timestamp"] > after
        ]

    return {
        "success": True,
        "messages": result
    }


@app.post("/chat")
def send_chat(
    data: ChatMessage,
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)
    cleanup_messages()

    sender = data.sender.strip()
    message = data.message.strip()

    if not sender:
        raise HTTPException(400, "Empty sender")

    if not message:
        raise HTTPException(400, "Empty message")

    if len(message) > MAX_MESSAGE_LENGTH:
        raise HTTPException(
            400,
            f"Message too long. Maximum is {MAX_MESSAGE_LENGTH} characters."
        )

    entry = {
        "id": uuid.uuid4().hex,
        "sender": sender[:32],
        "user_id": data.user_id,
        "message": message,
        "timestamp": time.time()
    }

    with messages_lock:
        messages.append(entry)

    return {
        "success": True,
        "message": entry
    }


@app.delete("/chat")
def clear_chat(
    x_api_key: Optional[str] = Header(default=None)
):
    check_api_key(x_api_key)

    with messages_lock:
        messages.clear()

    return {
        "success": True
    }


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        reload=False
    )
