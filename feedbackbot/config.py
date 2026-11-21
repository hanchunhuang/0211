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

    @property
    def db_path(self) -> Path:
        return self.data_dir / "feedback.db"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    def system_context(self) -> str:
        if self.system_context_file.exists():
            return self.system_context_file.read_text(encoding="utf-8")
        return "（未提供系統說明）"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
