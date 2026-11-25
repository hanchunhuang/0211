"""唯讀存取被回饋的系統程式碼（本機 git clone）。

用途：
- 分析問題時讓 AI 搜尋／閱讀相關程式碼，指出要改哪些檔案
- 每天讀取新的 commit（feat:/fix:），自動配對到已回報的問題
- 讀取客服知識庫（support_bot_knowledge.json）的「主選單 → 子選單 → 按鈕」路徑，
  讓公告能告訴使用者「在哪裡」

本模組只執行唯讀的 git 指令（fetch / reset 到遠端分支 / log / grep / show），絕不 commit 或 push。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

MAX_GREP_LINES = 80
MAX_READ_LINES = 250
SKIP_DIRS = ("vendor/", "node_modules/", "public/js/", "public/css/", "storage/", "public/vendor/")


@dataclass
class Commit:
    hash: str
    date: str
    subject: str
    body: str
    files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"hash": self.hash[:10], "date": self.date, "subject": self.subject,
                "body": self.body[:800], "files": self.files[:30]}


class CodeRepo:
    CLONE_DEPTH = 300

    def __init__(self, path: Path | str, branch: str = "master", url: str = ""):
        self.path = Path(path).resolve()
        self.branch = branch
        self.url = url
        self._knowledge: list[dict] | None = None
        self._clone_failed = False

    # ---------------- 基本 ----------------
    def exists(self) -> bool:
        """程式庫可用嗎？有設定 url 而本機還沒有時，會先自動 clone（淺層，只取最近的歷史）。"""
        if (self.path / ".git").exists():
            return True
        if self.url and not self._clone_failed:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                branch = ["--branch", self.branch] if self.branch else []  # 沒指定就用 repo 的預設分支
                r = subprocess.run(["git", "clone", "--quiet", f"--depth={self.CLONE_DEPTH}", *branch,
                                    self.url, str(self.path)], capture_output=True, text=True, timeout=600)
                if r.returncode != 0:
                    # 錯誤訊息可能含網址（裡面有 Token），只記錄前段且遮蔽
                    log.error("clone 失敗：%s", r.stderr.replace(self.url, "<REPO_URL>")[:300])
                    self._clone_failed = True
            except (OSError, subprocess.SubprocessError):
                log.exception("clone 失敗")
                self._clone_failed = True
        ok = (self.path / ".git").exists()
        if ok and not self.branch:
            self.branch = self._git("rev-parse", "--abbrev-ref", "HEAD").strip() or "master"
        return ok

    def _git(self, *args: str, check: bool = True, timeout: int = 120) -> str:
        r = subprocess.run(["git", "-C", str(self.path), *args], capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        if check and r.returncode not in (0, 1):  # grep 沒找到回 1
            err = r.stderr.replace(self.url, "<REPO_URL>") if self.url else r.stderr
            raise RuntimeError(f"git {args[0]} 失敗：{err.strip()[:300]}")
        return r.stdout

    def update(self) -> None:
        """抓最新的遠端分支（唯讀；不保留本機修改）。"""
        if not self.exists():
            return
        shallow = (self.path / ".git" / "shallow").exists()
        depth = [f"--depth={self.CLONE_DEPTH}"] if shallow else []
        self._git("fetch", "--quiet", *depth, "origin", self.branch, timeout=300)
        self._git("reset", "--quiet", "--hard", f"origin/{self.branch}")
        self._knowledge = None

    def head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    # ---------------- commits ----------------
    def commits_since(self, since_hash: str | None, max_count: int = 200, days_if_new: int = 3) -> list[Commit]:
        """回傳 since_hash 之後的 commit（舊 → 新）；第一次沒有基準時取最近幾天。"""
        rng = [f"{since_hash}..HEAD"] if since_hash else [f"--since={days_if_new}.days", "HEAD"]
        try:
            hashes = self._git("log", "--no-merges", f"--max-count={max_count}", "--format=%H", *rng).split()
        except RuntimeError:
            # 上次的 commit 不在目前（淺層）歷史裡：改取最近幾天
            log.warning("找不到上次處理的 commit %s，改讀最近 %d 天", since_hash, days_if_new)
            hashes = self._git("log", "--no-merges", f"--max-count={max_count}", "--format=%H",
                               f"--since={days_if_new}.days", "HEAD").split()
        sep = "\x1f"
        commits = []
        for h in reversed(hashes):
            out = self._git("show", "--name-only", "--date=short", f"--format=%ad{sep}%s{sep}%b{sep}", h)
            d, s, b, files = out.split(sep, 3)
            commits.append(Commit(hash=h, date=d.strip(), subject=s.strip(), body=b.strip(),
                                  files=[f for f in files.splitlines() if f.strip()]))
        return commits

    # ---------------- 搜尋／閱讀 ----------------
    def search(self, pattern: str, path_glob: str = "") -> str:
        args = ["grep", "-n", "-I", "-i", "-E", "--max-count=20", "-e", pattern]
        if path_glob:
            args += ["--", path_glob]
        out = self._git(*args)
        lines = [ln[:240] for ln in out.splitlines() if not ln.startswith(SKIP_DIRS)]
        if not lines:
            return "（沒有找到）"
        more = f"\n…還有 {len(lines) - MAX_GREP_LINES} 行，請縮小範圍" if len(lines) > MAX_GREP_LINES else ""
        return "\n".join(lines[:MAX_GREP_LINES]) + more

    def list_files(self, path_glob: str) -> str:
        out = self._git("ls-files", "--", path_glob)
        files = [f for f in out.splitlines() if not f.startswith(SKIP_DIRS)]
        if not files:
            return "（沒有符合的檔案）"
        return "\n".join(files[:150]) + (f"\n…共 {len(files)} 個" if len(files) > 150 else "")

    def read(self, rel_path: str, start: int = 1, end: int | None = None) -> str:
        p = (self.path / rel_path).resolve()
        if self.path not in p.parents or not p.is_file():
            return "（檔案不存在或不在專案內）"
        if p.name.startswith(".env"):
            return "（不提供環境設定檔）"
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start)
        end = min(len(lines), end or start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
        return f"{rel_path}（共 {len(lines)} 行，顯示 {start}-{end}）\n{body}"

    def show_commit(self, commit_hash: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{6,40}", commit_hash):
            return "（commit 編號格式不對）"
        out = self._git("show", "--stat", "--format=%H%n%ad%n%s%n%n%b", "--date=short", commit_hash)
        diff = self._git("show", "--format=", "--unified=2", commit_hash)
        if len(diff) > 12000:
            diff = diff[:12000] + "\n…（diff 過長已截斷）"
        return out + "\n" + diff

    # ---------------- 文件與知識庫 ----------------
    def read_docs(self, rel_paths: list[str], max_chars: int = 40000) -> str:
        parts = []
        for rel in rel_paths:
            p = self.path / rel
            if p.is_file():
                parts.append(f"## {rel}\n" + p.read_text(encoding="utf-8", errors="replace"))
        text = "\n\n".join(parts)
        return text[:max_chars]

    def knowledge(self, rel_path: str) -> list[dict]:
        if self._knowledge is None:
            p = self.path / rel_path
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self._knowledge = data if isinstance(data, list) else data.get("entries", [])
            except (OSError, ValueError):
                self._knowledge = []
        return self._knowledge

    def search_knowledge(self, rel_path: str, keywords: str, limit: int = 6) -> str:
        words = [w for w in re.split(r"[\s,，、]+", keywords) if w]
        scored = []
        for e in self.knowledge(rel_path):
            hay = f"{e.get('module', '')} {e.get('title', '')} {e.get('content', '')}"
            score = sum(hay.count(w) for w in words)
            if score:
                scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return "（知識庫沒有相關條目）"
        return "\n\n".join(
            f"【{e.get('module', '')}】{e.get('title', '')}\n{str(e.get('content', ''))[:1200]}" for _, e in scored[:limit])
