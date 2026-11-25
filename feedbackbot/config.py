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
    # ---- 資料庫與檔案 ----
    # firestore（Firebase 正式環境）或 sqlite（本機／一般主機）
    db_backend: str = field(default_factory=lambda: os.getenv("DB_BACKEND", "sqlite").strip().lower())
    firebase_project_id: str = field(default_factory=lambda: os.getenv("FIREBASE_PROJECT_ID", ""))
    firestore_prefix: str = field(default_factory=lambda: os.getenv("FIRESTORE_PREFIX", "fbot_"))
    # 有填就把截圖存到 Firebase Storage，例如 my-project.firebasestorage.app
    storage_bucket: str = field(default_factory=lambda: os.getenv("FIREBASE_STORAGE_BUCKET", ""))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "./data")))
    system_context_file: Path = field(
        default_factory=lambda: Path(os.getenv("SYSTEM_CONTEXT_FILE", "./system_context.md"))
    )
    max_images_per_group: int = field(default_factory=lambda: int(os.getenv("MAX_IMAGES_PER_GROUP", "20")))
    # 客服人員（例如在群組回答問題的同事）的 LINE userId，逐字稿會標註「客服」
    staff_user_ids: list[str] = field(
        default_factory=lambda: [x.strip() for x in os.getenv("STAFF_USER_IDS", "").split(",") if x.strip()]
    )
    # ---- 被回饋的系統程式碼（唯讀 git clone）----
    # 系統清單（每個系統的名稱、說明、Bitbucket repo）。檔案不存在時視為單一系統
    systems_file: str = field(default_factory=lambda: os.getenv("SYSTEMS_FILE", "systems.json"))
    # 各系統程式碼 clone 到這個資料夾底下（每個系統一個子資料夾）
    repos_dir: str = field(default_factory=lambda: os.getenv("REPOS_DIR", "./repos"))
    repo_path: str = field(default_factory=lambda: os.getenv("REPO_PATH", ""))
    # 選填：REPO_PATH 不存在時，用這個網址自動 clone（Cloud Run 這類沒有永久磁碟的環境用）
    repo_url: str = field(default_factory=lambda: os.getenv("REPO_URL", ""))
    repo_branch: str = field(default_factory=lambda: os.getenv("REPO_BRANCH", "master"))
    repo_auto_update: bool = field(default_factory=lambda: _bool(os.getenv("REPO_AUTO_UPDATE"), True))
    repo_context_docs: list[str] = field(default_factory=lambda: [
        x.strip() for x in os.getenv("REPO_CONTEXT_DOCS", "CLAUDE.md,docs/handover/AI_ONBOARDING.md").split(",")
        if x.strip()])
    repo_knowledge_file: str = field(default_factory=lambda: os.getenv(
        "REPO_KNOWLEDGE_FILE", "database/seeders/support_bot_knowledge.json"))
    # commit 配對信心門檻：AI 判斷 high 才自動標記完成
    commit_match_min_confidence: str = field(default_factory=lambda: os.getenv("COMMIT_MATCH_MIN_CONFIDENCE", "high"))
    # ---- 網頁後台 /admin（沒設密碼＝關閉）----
    admin_web_password: str = field(default_factory=lambda: os.getenv("ADMIN_WEB_PASSWORD", ""))
    # 對外網址（例如 https://feedback.hokiya.com），用於後台登入 cookie 與表單來源檢查
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "").rstrip("/"))
    session_secret: str = field(default_factory=lambda: os.getenv("SESSION_SECRET", ""))
    # ---- 排程 ----
    # true：程式內建排程（一般主機）；false：由外部（Cloud Scheduler）呼叫 /tasks/*
    enable_scheduler: bool = field(default_factory=lambda: _bool(os.getenv("ENABLE_SCHEDULER"), True))
    # 外部排程呼叫 /tasks/* 時要帶的密鑰（標頭 X-Task-Token）
    task_token: str = field(default_factory=lambda: os.getenv("TASK_TOKEN", ""))
    # ---- Telegram 通知（選填，省 LINE 推播額度）----
    telegram_bot_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "feedback.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    def make_db(self):
        from .db import DB
        from .store import FirestoreStore, SqliteStore

        if self.db_backend == "firestore":
            return DB(FirestoreStore(self.firebase_project_id or None, self.firestore_prefix))
        if self.db_backend != "sqlite":
            raise ValueError(f"DB_BACKEND 只能是 firestore 或 sqlite，目前是 {self.db_backend}")
        return DB(SqliteStore(self.db_path))

    def make_files(self):
        from .files import GcsFiles, LocalFiles

        if self.storage_bucket:
            return GcsFiles(self.storage_bucket)
        return LocalFiles(self.images_dir)

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
        if self.db_backend == "sqlite" or not self.storage_bucket:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.images_dir.mkdir(parents=True, exist_ok=True)
