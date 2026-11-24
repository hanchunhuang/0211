from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class Settings:
    line_channel_secret: str = field(default_factory=lambda: os.getenv("LINE_CHANNEL_SECRET", ""))
    line_access_token: str = field(default_factory=lambda: os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""))
    admin_user_ids: list[str] = field(
        default_factory=lambda: [x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]
    )
    claude_model: str = field(default_factory=lambda: os.getenv("CLAUDE_MODEL", "claude-opus-5-5"))
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Taipei"))
    digest_at: str = field(default_factory=lambda: os.getenv("DAILY_DIGEST_AT", "23:30"))
    announce_at: str = field(default_factory=lambda: os.getenv("DAILY_ANNOUNCE_AT", "09:00"))
    auto_push: bool = field(default_factory=lambda: _bool(os.getenv("AUTO_PUSH"), False))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))
    reports_dir: Path = field(default_factory=lambda: Path(os.getenv("REPORTS_DIR", "./reports")))
    system_context_file: Path = field(
        default_factory=lambda: Path(os.getenv("SYSTEM_CONTEXT_FILE", "./system_context.md"))
    )
    max_images_per_group: int = field(default_factory=lambda: int(os.getenv("MAX_IMAGES_PER_GROUP", "20")))
    # 客服人員（例如在群組回答問題的同事）的 LINE userId，逐字稿會標註「客服」
    staff_user_ids: list[str] = field(
        default_factory=lambda: [x.strip() for x in os.getenv("STAFF_USER_IDS", "").split(",") if x.strip()]
    )
    # ---- 被回饋的系統程式碼（唯讀 git clone）----
    repo_path: str = field(default_factory=lambda: os.getenv("REPO_PATH", ""))
    repo_branch: str = field(default_factory=lambda: os.getenv("REPO_BRANCH", "master"))
    repo_auto_update: bool = field(default_factory=lambda: _bool(os.getenv("REPO_AUTO_UPDATE"), True))
    repo_context_docs: list[str] = field(default_factory=lambda: [
        x.strip() for x in os.getenv("REPO_CONTEXT_DOCS", "CLAUDE.md,docs/handover/AI_ONBOARDING.md").split(",")
        if x.strip()])
    repo_knowledge_file: str = field(default_factory=lambda: os.getenv(
        "REPO_KNOWLEDGE_FILE", "database/seeders/support_bot_knowledge.json"))
    # commit 配對信心門檻：AI 判斷 high 才自動標記完成
    commit_match_min_confidence: str = field(default_factory=lambda: os.getenv("COMMIT_MATCH_MIN_CONFIDENCE", "high"))
    # ---- Telegram 通知（選填，省 LINE 推播額度）----
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "feedback.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    def system_context(self, repo=None) -> str:
        """給 AI 的系統背景：自訂說明檔 + 程式庫內的交接文件（CLAUDE.md 等）。"""
        parts = []
        if self.system_context_file.exists():
            parts.append(self.system_context_file.read_text(encoding="utf-8"))
        if repo is not None and repo.exists():
            docs = repo.read_docs(self.repo_context_docs)
            if docs:
                parts.append(docs)
        return "\n\n".join(parts) or "（未提供系統說明）"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
