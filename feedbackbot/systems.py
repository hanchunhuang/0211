"""多系統設定：回饋屬於哪一個系統、每個系統的程式碼在哪裡。

系統清單寫在 systems.json（不含任何密碼），每個系統的讀取權杖放在環境變數：
  REPO_TOKEN_<代號大寫>   例如 REPO_TOKEN_HCARE（Bitbucket Repository access token）
  REPO_USER_<代號大寫>    選填，預設 x-token-auth（若用個人 API token 就填使用者名稱）
  REPO_TOKEN / REPO_USER  選填，所有系統共用的權杖（例如可讀取全部 repo 的個人 API token）
  REPO_URL_<代號大寫>     選填，直接給完整 clone 網址（含權杖）
相容舊設定：第一個系統可沿用 REPO_URL / REPO_PATH。
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from .repo import CodeRepo

log = logging.getLogger(__name__)
UNKNOWN = "unknown"
UNKNOWN_NAME = "未分類"
DEFAULT_DOCS = ["CLAUDE.md", "README.md", "docs/handover/AI_ONBOARDING.md"]


@dataclass
class SystemDef:
    id: str
    name: str
    description: str = ""
    repo: str = ""                    # Bitbucket「工作區/repo」，例如 hokiya/line-hokiya-survey
    branch: str = ""                  # 空白＝repo 的預設分支
    host: str = "bitbucket.org"
    knowledge_file: str = ""          # 選填：使用者操作說明（含選單路徑）的 JSON
    context_docs: list[str] = field(default_factory=lambda: list(DEFAULT_DOCS))
    keywords: list[str] = field(default_factory=list)
    path: str = ""                    # 選填：本機已 clone 好的路徑
    public: bool = False              # 公開 repo（不需要權杖）


def _env_key(system_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", system_id).upper()


class Systems:
    def __init__(self, defs: list[SystemDef], settings, repos: dict[str, CodeRepo] | None = None):
        self.defs = {d.id: d for d in defs}
        self.settings = settings
        self._repos: dict[str, CodeRepo | None] = dict(repos or {})
        self._profiles: dict[str, str] = {}

    # ------------------------------------------------------------ 建立
    @classmethod
    def from_settings(cls, settings) -> "Systems":
        path = Path(settings.systems_file)
        if path.is_file():
            raw = json.loads(path.read_text(encoding="utf-8"))
            defs = [SystemDef(**{k: v for k, v in s.items() if k in SystemDef.__dataclass_fields__}) for s in raw]
            return cls(defs, settings)
        # 沒有 systems.json：單一系統（舊設定）
        return cls([SystemDef(id="default", name="系統", knowledge_file=settings.repo_knowledge_file,
                              context_docs=settings.repo_context_docs)], settings)

    @classmethod
    def single(cls, settings, repo: CodeRepo | None, system_id: str = "default", name: str = "系統") -> "Systems":
        return cls([SystemDef(id=system_id, name=name, knowledge_file=settings.repo_knowledge_file,
                              context_docs=settings.repo_context_docs)], settings, {system_id: repo})

    # ------------------------------------------------------------ 查詢
    def ids(self) -> list[str]:
        return list(self.defs)

    def get(self, system_id: str | None) -> SystemDef | None:
        return self.defs.get(system_id or "")

    def name(self, system_id: str | None) -> str:
        d = self.get(system_id)
        return d.name if d else UNKNOWN_NAME

    def valid(self, system_id: str | None) -> bool:
        return bool(system_id) and system_id in self.defs

    def options(self) -> list[tuple[str, str]]:
        return [(d.id, d.name) for d in self.defs.values()]

    # ------------------------------------------------------------ 程式庫
    def clone_url(self, d: SystemDef) -> str:
        key = _env_key(d.id)
        url = os.getenv(f"REPO_URL_{key}", "")
        if url:
            return url
        first = next(iter(self.defs)) == d.id
        if first and self.settings.repo_url and not os.getenv(f"REPO_TOKEN_{key}"):
            return self.settings.repo_url
        if not d.repo:
            return ""
        token = os.getenv(f"REPO_TOKEN_{key}") or os.getenv("REPO_TOKEN", "")
        user = os.getenv(f"REPO_USER_{key}") or os.getenv("REPO_USER", "") or "x-token-auth"
        if not token:
            return f"https://{d.host}/{d.repo}.git" if d.public else ""  # 私有 repo 沒權杖就不嘗試
        # 權杖可能含特殊字元，要編碼後才能放進網址
        return f"https://{quote(user, safe='')}:{quote(token, safe='')}@{d.host}/{d.repo}.git"

    def repo(self, system_id: str | None) -> CodeRepo | None:
        if not self.valid(system_id):
            return None
        if system_id not in self._repos:
            d = self.defs[system_id]
            first = next(iter(self.defs)) == d.id
            local = d.path or (self.settings.repo_path if first and self.settings.repo_path else "")
            path = local or str(Path(self.settings.repos_dir) / d.id)
            url = self.clone_url(d)
            if not url and not Path(path, ".git").exists():
                self._repos[system_id] = None
            else:
                self._repos[system_id] = CodeRepo(path, d.branch, url=url)
        return self._repos[system_id]

    def knowledge_file(self, system_id: str | None) -> str:
        d = self.get(system_id)
        return d.knowledge_file if d else ""

    # ------------------------------------------------------------ 給 AI 的說明
    def context(self, system_id: str | None) -> str:
        """分析某個系統的問題時給 AI 的背景：系統簡介＋補充說明檔＋程式庫內的交接文件。"""
        d = self.get(system_id)
        parts = []
        if d:
            parts.append(f"# 這個問題屬於：{d.name}\n{d.description}")
        else:
            parts.append("# 這個問題還沒判斷出屬於哪個系統。可能的系統：\n" + self.catalog())
        if self.settings.system_context_file.exists():
            parts.append(self.settings.system_context_file.read_text(encoding="utf-8"))
        repo = self.repo(system_id)
        if d and repo is not None and repo.exists():
            docs = repo.read_docs(d.context_docs)
            if docs:
                parts.append(docs)
        return "\n\n".join(parts)

    def catalog(self, only: list[str] | None = None, with_readme: bool = False) -> str:
        """系統清單（給 AI 判斷回饋屬於哪個系統）。"""
        lines = []
        for d in self.defs.values():
            if only and d.id not in only:
                continue
            kw = f"（關鍵字：{'、'.join(d.keywords)}）" if d.keywords else ""
            lines.append(f"- {d.id}：{d.name}。{d.description}{kw}")
            if with_readme:
                prof = self._profile(d.id)
                if prof:
                    lines.append("  簡介摘錄：" + prof.replace("\n", " ")[:600])
        return "\n".join(lines)

    def _profile(self, system_id: str) -> str:
        if system_id not in self._profiles:
            text = ""
            repo = self.repo(system_id)
            try:
                if repo is not None and repo.exists():
                    text = repo.read_docs(["README.md", "CLAUDE.md"], max_chars=1500)
            except Exception:  # noqa: BLE001
                log.warning("讀取 %s 簡介失敗", system_id, exc_info=True)
            self._profiles[system_id] = text
        return self._profiles[system_id]
