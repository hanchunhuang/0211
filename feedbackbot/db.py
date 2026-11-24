"""SQLite 儲存層。所有時間以 ISO 字串（含時區）儲存。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id               TEXT PRIMARY KEY,          -- LINE groupId / roomId
    company          TEXT,                      -- 公司名稱（可用 /company 指令修改）
    name             TEXT,                      -- LINE 群組名稱
    active           INTEGER NOT NULL DEFAULT 1,
    joined_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    line_message_id  TEXT UNIQUE,
    group_id         TEXT NOT NULL,
    user_id          TEXT,
    display_name     TEXT,
    kind             TEXT NOT NULL,             -- text / image / file / other
    text             TEXT,
    image_path       TEXT,
    quoted_message_id TEXT,
    created_at       TEXT NOT NULL,
    processed        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_unprocessed ON messages(processed, group_id);

CREATE TABLE IF NOT EXISTS issues (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    title            TEXT NOT NULL,
    kind             TEXT NOT NULL,             -- bug / feature / usability / question
    summary          TEXT NOT NULL,
    module           TEXT,
    status           TEXT NOT NULL DEFAULT 'open',  -- open / analyzed / in_progress / resolved / wontfix
    priority         TEXT,                      -- P0..P3
    analysis         TEXT,                      -- JSON
    resolution_note  TEXT,                      -- 怎麼改的
    resolution_location TEXT,                   -- 改在哪裡（使用者看得懂的位置）
    resolved_at      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         TEXT NOT NULL,
    issue_id         INTEGER REFERENCES issues(id),
    message_ids      TEXT NOT NULL,             -- JSON list of messages.id
    reporter         TEXT,
    kind             TEXT NOT NULL,
    title            TEXT NOT NULL,
    detail           TEXT NOT NULL,
    severity         TEXT,
    digest_date      TEXT NOT NULL,             -- YYYY-MM-DD
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_issue ON feedback(issue_id);

CREATE TABLE IF NOT EXISTS announcements (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id         TEXT NOT NULL,
    for_date         TEXT NOT NULL,
    text             TEXT NOT NULL,
    issue_ids        TEXT NOT NULL,             -- JSON list
    status           TEXT NOT NULL DEFAULT 'draft', -- draft / sent / skipped / failed
    error            TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(issues)")}
        if "fix_commits" not in cols:
            self.conn.execute("ALTER TABLE issues ADD COLUMN fix_commits TEXT")  # JSON list of commit hash
        if "auto_resolved" not in cols:
            self.conn.execute("ALTER TABLE issues ADD COLUMN auto_resolved INTEGER NOT NULL DEFAULT 0")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    # ---------- groups ----------
    def upsert_group(self, group_id: str, now: datetime, name: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO groups(id, name, company, active, joined_at) VALUES (?,?,?,1,?)
                   ON CONFLICT(id) DO UPDATE SET active=1, name=COALESCE(excluded.name, groups.name)""",
                (group_id, name, name, now.isoformat()),
            )

    def set_group_company(self, group_id: str, company: str) -> bool:
        with self.tx() as c:
            cur = c.execute("UPDATE groups SET company=? WHERE id=?", (company, group_id))
            return cur.rowcount > 0

    def deactivate_group(self, group_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE groups SET active=0 WHERE id=?", (group_id,))

    def group_label(self, group_id: str) -> str:
        r = self.one("SELECT company, name FROM groups WHERE id=?", (group_id,))
        if not r:
            return group_id[-6:]
        return r["company"] or r["name"] or group_id[-6:]

    # ---------- messages ----------
    def add_message(self, **m: Any) -> bool:
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO messages
                   (line_message_id, group_id, user_id, display_name, kind, text, image_path,
                    quoted_message_id, created_at)
                   VALUES (:line_message_id, :group_id, :user_id, :display_name, :kind, :text,
                           :image_path, :quoted_message_id, :created_at)""",
                {
                    "line_message_id": m.get("line_message_id"),
                    "group_id": m["group_id"],
                    "user_id": m.get("user_id"),
                    "display_name": m.get("display_name"),
                    "kind": m["kind"],
                    "text": m.get("text"),
                    "image_path": m.get("image_path"),
                    "quoted_message_id": m.get("quoted_message_id"),
                    "created_at": m["created_at"],
                },
            )
            return cur.rowcount > 0

    def unprocessed_messages(self) -> dict[str, list[sqlite3.Row]]:
        rows = self.q("SELECT * FROM messages WHERE processed=0 ORDER BY group_id, id")
        out: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            out.setdefault(r["group_id"], []).append(r)
        return out

    def mark_processed(self, ids: list[int]) -> None:
        if not ids:
            return
        with self.tx() as c:
            c.executemany("UPDATE messages SET processed=1 WHERE id=?", [(i,) for i in ids])

    # ---------- issues ----------
    def open_issues(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM issues WHERE status NOT IN ('resolved','wontfix') ORDER BY id")

    def issue(self, issue_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM issues WHERE id=?", (issue_id,))

    def create_issue(self, title: str, kind: str, summary: str, module: str | None, now: datetime) -> int:
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO issues(title, kind, summary, module, status, created_at, updated_at)
                   VALUES (?,?,?,?, 'open', ?, ?)""",
                (title, kind, summary, module, now.isoformat(), now.isoformat()),
            )
            return int(cur.lastrowid)

    def update_issue(self, issue_id: int, now: datetime, **fields: Any) -> None:
        if not fields:
            return
        if "analysis" in fields and not isinstance(fields["analysis"], (str, type(None))):
            fields["analysis"] = json.dumps(fields["analysis"], ensure_ascii=False)
        fields["updated_at"] = now.isoformat()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE issues SET {cols} WHERE id=?", (*fields.values(), issue_id))

    def issue_groups(self, issue_id: int) -> list[str]:
        return [r["group_id"] for r in self.q(
            "SELECT DISTINCT group_id FROM feedback WHERE issue_id=? ORDER BY group_id", (issue_id,))]

    def issue_feedback(self, issue_id: int) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM feedback WHERE issue_id=? ORDER BY id", (issue_id,))

    # ---------- feedback ----------
    def add_feedback(self, *, group_id: str, message_ids: list[int], reporter: str | None, kind: str,
                     title: str, detail: str, severity: str | None, digest_date: str, now: datetime) -> int:
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO feedback(group_id, message_ids, reporter, kind, title, detail, severity,
                                        digest_date, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (group_id, json.dumps(message_ids), reporter, kind, title, detail, severity,
                 digest_date, now.isoformat()),
            )
            return int(cur.lastrowid)

    def link_feedback(self, feedback_id: int, issue_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE feedback SET issue_id=? WHERE id=?", (issue_id, feedback_id))

    def unlinked_feedback(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM feedback WHERE issue_id IS NULL ORDER BY id")

    # ---------- announcements ----------
    def announced_issue_ids(self, group_id: str) -> set[int]:
        ids: set[int] = set()
        for r in self.q("SELECT issue_ids FROM announcements WHERE group_id=? AND status='sent'", (group_id,)):
            ids.update(json.loads(r["issue_ids"]))
        return ids

    def add_announcement(self, group_id: str, for_date: str, text: str, issue_ids: list[int], now: datetime) -> int:
        with self.tx() as c:
            # 同一天同群組只保留最新一份草稿
            c.execute("UPDATE announcements SET status='skipped' WHERE group_id=? AND for_date=? AND status='draft'",
                      (group_id, for_date))
            cur = c.execute(
                "INSERT INTO announcements(group_id, for_date, text, issue_ids, status, created_at) VALUES (?,?,?,?, 'draft', ?)",
                (group_id, for_date, text, json.dumps(issue_ids), now.isoformat()),
            )
            return int(cur.lastrowid)

    def drafts(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM announcements WHERE status IN ('draft','failed') ORDER BY id")

    def set_announcement_status(self, ann_id: int, status: str, now: datetime, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE announcements SET status=?, error=?, sent_at=? WHERE id=?",
                      (status, error, now.isoformat() if status == "sent" else None, ann_id))

    # ---------- kv ----------
    def get_kv(self, key: str) -> str | None:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else None

    def set_kv(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO kv(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))
