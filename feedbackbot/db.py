"""資料存取層：所有讀寫都經過這裡，底層可以是 Firestore 或 SQLite（見 store.py）。

集合：
  groups         LINE 群組（文件 id = LINE groupId）
  messages       收集到的訊息（id 為流水號）
  feedback       從訊息萃取出的回饋
  issues         歸類後的問題
  announcements  修正公告
  reports        日報／成效評估（文件 id = 檔名）
  kv             其他狀態（上次評估時間、上次處理的 commit…）

時間一律存成含時區的 ISO 字串（同一時區），可以直接用字串比較大小。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .store import DocStore, SqliteStore

CLOSED = ("resolved", "wontfix")


class DB:
    def __init__(self, store: DocStore | str):
        # 相容舊用法：DB(":memory:") 或 DB(路徑) → SQLite
        self.s: DocStore = SqliteStore(store) if isinstance(store, str) or hasattr(store, "__fspath__") else store

    # ================================================================ groups
    def upsert_group(self, group_id: str, now: datetime, name: str | None = None) -> None:
        cur = self.s.get("groups", group_id)
        if cur:
            fields: dict[str, Any] = {"active": True}
            if name:
                fields["name"] = name
            self.s.update("groups", group_id, fields)
        else:
            self.s.set("groups", group_id, {"id": group_id, "name": name, "company": name, "active": True,
                                            "joined_at": now.isoformat(), "msg_count": 0, "last_msg_at": None})

    def group(self, group_id: str) -> dict | None:
        return self.s.get("groups", group_id)

    def groups(self, active_only: bool = False) -> list[dict]:
        rows = self.s.find("groups")
        if active_only:
            rows = [g for g in rows if g.get("active")]
        return sorted(rows, key=lambda g: g.get("joined_at") or "")

    def find_group_by_suffix(self, code: str) -> dict | None:
        return next((g for g in self.groups() if code and g["id"].endswith(code)), None)

    def set_group_company(self, group_id: str, company: str) -> bool:
        if not self.group(group_id):
            return False
        self.s.update("groups", group_id, {"company": company})
        return True

    def deactivate_group(self, group_id: str) -> None:
        if self.group(group_id):
            self.s.update("groups", group_id, {"active": False})

    def group_label(self, group_id: str) -> str:
        g = self.group(group_id) if group_id else None
        if not g:
            return (group_id or "")[-6:]
        return g.get("company") or g.get("name") or group_id[-6:]

    # ================================================================ messages
    def add_message(self, **m: Any) -> bool:
        """新增訊息；同一則 LINE 訊息重送時不重複存。"""
        line_id = m.get("line_message_id")
        if line_id and self.s.find("messages", [("line_message_id", "==", line_id)], limit=1):
            return False
        mid = self.s.next_id("messages")
        self.s.set("messages", mid, {
            "id": mid,
            "line_message_id": line_id,
            "group_id": m["group_id"],
            "user_id": m.get("user_id"),
            "display_name": m.get("display_name"),
            "kind": m["kind"],
            "text": m.get("text"),
            "image_ref": m.get("image_ref"),
            "quoted_message_id": m.get("quoted_message_id"),
            "created_at": m["created_at"],
            "processed": False,
        })
        if self.group(m["group_id"]):
            self.s.increment("groups", m["group_id"], "msg_count")
            self.s.update("groups", m["group_id"], {"last_msg_at": m["created_at"]})
        return True

    def message(self, mid: int) -> dict | None:
        return self.s.get("messages", mid)

    def messages_by_ids(self, ids: list[int]) -> list[dict]:
        return sorted(self.s.find("messages", [("id", "in", list(ids))]), key=lambda m: m["id"]) if ids else []

    def unprocessed_messages(self) -> dict[str, list[dict]]:
        rows = sorted(self.s.find("messages", [("processed", "==", False)]), key=lambda m: m["id"])
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(r["group_id"], []).append(r)
        return out

    def mark_processed(self, ids: list[int]) -> None:
        for i in ids:
            self.s.update("messages", i, {"processed": True})

    def count_messages(self, since: str | None = None, group_id: str | None = None,
                       unprocessed: bool = False) -> int:
        where: list = []
        if group_id:
            where.append(("group_id", "==", group_id))
        if since:
            where.append(("created_at", ">=", since))
        if unprocessed:
            where.append(("processed", "==", False))
        return self.s.count("messages", where)

    def message_times(self, since: str, group_id: str | None = None) -> list[str]:
        where: list = [("created_at", ">=", since)]
        if group_id:
            where.insert(0, ("group_id", "==", group_id))
        return [m["created_at"] for m in self.s.find("messages", where, fields=["created_at"])]

    def messages_page(self, group_id: str = "", q: str = "", page: int = 1,
                      size: int = 100, search_window: int = 3000) -> tuple[list[dict], int]:
        """分頁列出訊息（新到舊）。有關鍵字時在最近 search_window 則中搜尋。"""
        where = [("group_id", "==", group_id)] if group_id else []
        page = max(1, page)
        if q:
            recent = self.s.find("messages", where, order_by="id", desc=True, limit=search_window)
            ql = q.lower()
            hits = [m for m in recent if ql in (m.get("text") or "").lower() or ql in (m.get("display_name") or "").lower()]
            return hits[(page - 1) * size: page * size], len(hits)
        total = self.s.count("messages", where)
        rows = self.s.find("messages", where, order_by="id", desc=True, limit=size, offset=(page - 1) * size)
        return rows, total

    # ================================================================ issues
    def all_issues(self) -> list[dict]:
        return sorted(self.s.find("issues"), key=lambda i: i["id"])

    def open_issues(self) -> list[dict]:
        return [i for i in self.all_issues() if i["status"] not in CLOSED]

    def issue(self, issue_id: int) -> dict | None:
        return self.s.get("issues", int(issue_id)) if issue_id else None

    def create_issue(self, title: str, kind: str, summary: str, module: str | None, now: datetime) -> int:
        iid = self.s.next_id("issues")
        self.s.set("issues", iid, {
            "id": iid, "title": title, "kind": kind, "summary": summary, "module": module, "status": "open",
            "priority": None, "analysis": None, "resolution_note": None, "resolution_location": None,
            "resolved_at": None, "fix_commits": None, "auto_resolved": 0,
            "created_at": now.isoformat(), "updated_at": now.isoformat(),
        })
        return iid

    def update_issue(self, issue_id: int, now: datetime, **fields: Any) -> None:
        if not fields:
            return
        if "analysis" in fields and not isinstance(fields["analysis"], (str, type(None))):
            fields["analysis"] = json.dumps(fields["analysis"], ensure_ascii=False)
        fields["updated_at"] = now.isoformat()
        self.s.update("issues", int(issue_id), fields)

    def issues_resolved_since(self, since_iso: str) -> list[dict]:
        return [i for i in self.all_issues() if i["status"] in CLOSED and (i.get("resolved_at") or "") > since_iso]

    def issue_groups(self, issue_id: int) -> list[str]:
        return sorted({f["group_id"] for f in self.issue_feedback(issue_id)})

    def issue_feedback(self, issue_id: int) -> list[dict]:
        return sorted(self.s.find("feedback", [("issue_id", "==", int(issue_id))]), key=lambda f: f["id"])

    def issues_for_group(self, group_id: str) -> list[dict]:
        ids = {f["issue_id"] for f in self.feedback_for_group(group_id) if f.get("issue_id")}
        return [i for i in self.all_issues() if i["id"] in ids]

    # ================================================================ feedback
    def add_feedback(self, *, group_id: str, message_ids: list[int], reporter: str | None, kind: str,
                     title: str, detail: str, severity: str | None, digest_date: str, now: datetime) -> int:
        fid = self.s.next_id("feedback")
        self.s.set("feedback", fid, {
            "id": fid, "group_id": group_id, "issue_id": None, "message_ids": json.dumps(message_ids),
            "reporter": reporter, "kind": kind, "title": title, "detail": detail, "severity": severity,
            "digest_date": digest_date, "created_at": now.isoformat(),
        })
        return fid

    def link_feedback(self, feedback_id: int, issue_id: int) -> None:
        self.s.update("feedback", feedback_id, {"issue_id": int(issue_id)})

    def all_feedback(self) -> list[dict]:
        return sorted(self.s.find("feedback"), key=lambda f: f["id"])

    def unlinked_feedback(self) -> list[dict]:
        return [f for f in self.all_feedback() if not f.get("issue_id")]

    def feedback_for_group(self, group_id: str) -> list[dict]:
        return sorted(self.s.find("feedback", [("group_id", "==", group_id)]), key=lambda f: f["id"])

    # ================================================================ announcements
    def announcements(self) -> list[dict]:
        return sorted(self.s.find("announcements"), key=lambda a: a["id"], reverse=True)

    def announcement(self, ann_id: int) -> dict | None:
        return self.s.get("announcements", int(ann_id))

    def announced_issue_ids(self, group_id: str) -> set[int]:
        ids: set[int] = set()
        for a in self.s.find("announcements", [("group_id", "==", group_id)]):
            if a["status"] == "sent":
                ids.update(json.loads(a["issue_ids"]))
        return ids

    def add_announcement(self, group_id: str, for_date: str, text: str, issue_ids: list[int], now: datetime) -> int:
        # 同一天同群組只保留最新一份草稿
        for a in self.s.find("announcements", [("group_id", "==", group_id)]):
            if a["for_date"] == for_date and a["status"] == "draft":
                self.s.update("announcements", a["id"], {"status": "skipped"})
        aid = self.s.next_id("announcements")
        self.s.set("announcements", aid, {
            "id": aid, "group_id": group_id, "for_date": for_date, "text": text,
            "issue_ids": json.dumps(issue_ids), "status": "draft", "error": None,
            "created_at": now.isoformat(), "sent_at": None,
        })
        return aid

    def drafts(self) -> list[dict]:
        return sorted((a for a in self.announcements() if a["status"] in ("draft", "failed")), key=lambda a: a["id"])

    def set_announcement_status(self, ann_id: int, status: str, now: datetime, error: str | None = None) -> None:
        if not self.announcement(ann_id):
            return
        self.s.update("announcements", int(ann_id), {
            "status": status, "error": error, "sent_at": now.isoformat() if status == "sent" else None})

    # ================================================================ reports
    def save_report(self, name: str, content: str, now: datetime) -> None:
        self.s.set("reports", name, {"name": name, "content": content, "created_at": now.isoformat()})

    def report(self, name: str) -> dict | None:
        return self.s.get("reports", name) if name else None

    def report_names(self) -> list[str]:
        return sorted((r["name"] for r in self.s.find("reports", fields=["name"])), reverse=True)

    # ================================================================ kv
    def get_kv(self, key: str) -> str | None:
        r = self.s.get("kv", key)
        return r["value"] if r else None

    def set_kv(self, key: str, value: str) -> None:
        self.s.set("kv", key, {"value": value})
