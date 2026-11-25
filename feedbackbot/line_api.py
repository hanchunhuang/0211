"""LINE Messaging API 的最小封裝（直接呼叫 REST API）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

import httpx

log = logging.getLogger(__name__)

API = "https://api.line.me/v2/bot"
DATA_API = "https://api-data.line.me/v2/bot"
MAX_TEXT = 5000  # LINE 單則文字訊息上限


def verify_signature(channel_secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not channel_secret:
        return False
    digest = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """把長文字切成多則訊息（優先在換行處切）。"""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text.strip():
        chunks.append(text)
    return chunks


class LineClient:
    def __init__(self, access_token: str, timeout: float = 15.0):
        self.http = httpx.Client(
            headers={"Authorization": f"Bearer {access_token}"}, timeout=timeout
        )

    def _text_messages(self, text: str) -> list[dict]:
        return [{"type": "text", "text": t} for t in split_text(text)][:5]  # 一次最多 5 則

    def reply(self, reply_token: str, text: str) -> None:
        r = self.http.post(f"{API}/message/reply",
                           json={"replyToken": reply_token, "messages": self._text_messages(text)})
        if r.status_code >= 400:
            log.warning("reply failed %s %s", r.status_code, r.text)

    def push(self, to: str, text: str) -> None:
        """推播到使用者或群組（每次 API 呼叫最多 5 則）。失敗會丟例外，讓呼叫端記錄狀態。"""
        parts = split_text(text)
        for i in range(0, len(parts), 5):
            batch = [{"type": "text", "text": t} for t in parts[i:i + 5]]
            r = self.http.post(f"{API}/message/push", json={"to": to, "messages": batch})
            r.raise_for_status()

    def bot_info(self) -> dict:
        """機器人自己的資料（顯示名稱、頭像）。"""
        r = self.http.get(f"{API}/info")
        r.raise_for_status()
        return r.json()

    def group_name(self, group_id: str) -> str | None:
        r = self.http.get(f"{API}/group/{group_id}/summary")
        return r.json().get("groupName") if r.status_code == 200 else None

    def member_name(self, group_id: str, user_id: str) -> str | None:
        r = self.http.get(f"{API}/group/{group_id}/member/{user_id}")
        if r.status_code == 200:
            return r.json().get("displayName")
        return None

    def download_content(self, message_id: str) -> tuple[bytes, str] | None:
        """下載使用者傳的圖片／檔案。回傳 (內容, content-type)。"""
        r = self.http.get(f"{DATA_API}/message/{message_id}/content")
        if r.status_code != 200:
            log.warning("download content failed %s", r.status_code)
            return None
        return r.content, r.headers.get("content-type", "image/jpeg").split(";")[0]
