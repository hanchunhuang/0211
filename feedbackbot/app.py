"""Webhook 伺服器 + 每日排程。

啟動：python -m feedbackbot serve（或 uvicorn feedbackbot.app:app）
LINE Developers 的 Webhook URL 設為 https://你的網域/callback
在 Cloud Run 上：ENABLE_SCHEDULER=false，改由 Cloud Scheduler 呼叫 POST /tasks/digest、/tasks/announce
"""
from __future__ import annotations

import logging
import secrets
import threading

import httpx
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from . import commands, fixreport, pipeline
from .admin import LoginRequired, build_router, login_redirect_handler
from .config import Settings
from .db import DB
from .line_api import LineClient, verify_signature
from .llm import ClaudeLLM
from .systems import Systems

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("feedbackbot")


class Bot:
    def __init__(self, settings: Settings | None = None, db: DB | None = None, line=None, llm=None, repo=None,
                 files=None):
        self.s = settings or Settings()
        self.s.ensure_dirs()
        self.db = db or self.s.make_db()
        self.files = files or self.s.make_files()
        self.line = line or LineClient(self.s.line_access_token)
        self.llm = llm or ClaudeLLM(self.s.claude_model, knowledge_file=self.s.repo_knowledge_file)
        self.systems = Systems.single(self.s, repo) if repo is not None else Systems.from_settings(self.s)
        self.tz = ZoneInfo(self.s.timezone)
        self._names: dict[tuple[str, str], str] = {}
        # 這個服務自己的 Cloud Run 網址（第一次收到請求時記下），長時間工作用來呼叫自己
        self.self_url: str | None = self.s.self_url or None

    def now(self) -> datetime:
        return datetime.now(self.tz)

    # ---------------- 通知管理者 ----------------
    def notify_admins(self, text: str) -> None:
        """通知管理者：有設定 Telegram 就走 Telegram（不耗 LINE 推播額度），否則 LINE 私訊。"""
        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            try:
                for i in range(0, len(text), 4000):
                    httpx.post(f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                               json={"chat_id": self.s.telegram_chat_id, "text": text[i:i + 4000]},
                               timeout=15).raise_for_status()
                return
            except Exception:  # noqa: BLE001
                log.exception("telegram notify failed, fallback to LINE")
        for uid in self.s.admin_user_ids:
            try:
                self.line.push(uid, text)
            except Exception:  # noqa: BLE001
                log.exception("notify admin failed")

    # ---------------- 排程工作 ----------------
    def job_digest(self) -> str:
        try:
            res = pipeline.digest(self.db, self.llm, self.s, self.now(), files=self.files, systems=self.systems)
            msg = res.admin_summary + (f"\n\n完整日報：{self._report_link(res.report_name)}" if res.report_name else "")
        except Exception as e:  # noqa: BLE001
            log.exception("digest failed")
            msg = f"⚠️ 整理回饋失敗：{e}"
        self.notify_admins(msg)
        return msg

    def job_sync(self, notify: bool = True) -> str:
        if not self.s.code_analysis:
            msg = "目前是手動模式：David 不讀程式碼，也不自動配對 commit。處理好的問題請用 /resolve 編號 標記完成。"
            if notify:
                self.notify_admins(msg)
            return msg
        try:
            msg = pipeline.sync_commits(self.db, self.llm, self.s, now=self.now(), systems=self.systems).admin_summary
        except Exception as e:  # noqa: BLE001
            log.exception("sync commits failed")
            msg = f"⚠️ 讀取 commit 失敗：{e}"
        if notify:
            self.notify_admins(msg)
        return msg

    def job_analyze(self, notify: bool = True) -> str:
        try:
            msg = pipeline.analyze_requested(self.db, self.llm, self.s, self.now(), systems=self.systems)
        except Exception as e:  # noqa: BLE001
            log.exception("analyze failed")
            msg = f"⚠️ 分析失敗：{e}"
        if notify:
            self.notify_admins(msg)
        return msg

    def start_job(self, job: str) -> None:
        """在背景啟動長時間工作（Cloud Run 上會改呼叫自己的 /tasks/<job>）。"""
        _spawn(self._run_long, job, None, None)

    def job_announce(self) -> str:
        sync_msg = self.job_sync(notify=False) if self.s.code_analysis else ""
        try:
            res = pipeline.prepare_announcements(self.db, self.llm, self.s, self.now(), systems=self.systems)
            msg = (sync_msg + "\n\n" if sync_msg else "") + res.admin_summary
            if res.evaluation_name:
                msg += f"\n\n成效評估報告：{self._report_link(res.evaluation_name)}"
            if self.s.auto_push and res.draft_ids:
                results = pipeline.send_announcements(self.db, self.line, self.now(), res.draft_ids)
                msg += "\n\n" + "\n".join(f"草稿 {i}：{r}" for i, r in results)
        except Exception as e:  # noqa: BLE001
            log.exception("announce failed")
            msg = f"⚠️ 產生公告失敗：{e}"
        self.notify_admins(msg)
        return msg

    # ---------------- Webhook 事件 ----------------
    def handle_event(self, ev: dict) -> None:
        etype = ev.get("type")
        src = ev.get("source", {})
        chat_id = src.get("groupId") or src.get("roomId")
        ts = datetime.fromtimestamp(ev.get("timestamp", 0) / 1000, self.tz)

        if etype == "join" and chat_id:
            name = self._group_name(chat_id) if src.get("groupId") else None
            self.db.upsert_group(chat_id, ts, name)
            where = f"{self.s.public_base_url}/admin/groups/{chat_id}" if self.s.public_base_url else "後台「單位」頁"
            self.notify_admins(f"🤖 已加入群組「{name or chat_id[-8:]}」，開始收集。\n請設定這個群組對應的中心名稱：{where}\n（在後台設定，群組裡不會出現任何訊息）")
            return
        if etype == "leave" and chat_id:
            self.db.deactivate_group(chat_id, ts)
            return
        if etype != "message":
            return

        msg = ev["message"]
        user_id = src.get("userId")
        text = msg.get("text", "") if msg.get("type") == "text" else ""
        command = _as_command(text)
        is_admin = user_id in self.s.admin_user_ids
        reply_token = ev.get("replyToken")

        # ---- 1 對 1 私訊：只處理管理者指令 ----
        if src.get("type") == "user":
            if is_admin and fixreport.is_report(text):
                res = pipeline.apply_fix_report(self.db, self.systems, text, self.now(), "LINE")
                if res.ok:
                    msg = (f"✅ 已套用修改回報（{self.systems.name(res.system_id)}）\n"
                           + "\n".join(f"#{x['id']} {x['title']}：{x['how'][:60]}" for x in res.issues)
                           + "\n\n已記入系統修改紀錄。公告草稿明早產生，急的話私訊 /announce。")
                    if res.warnings:
                        msg += "\n\n提醒：" + "；".join(res.warnings)
                else:
                    msg = "⚠️ 修改回報還不能套用：\n" + "\n".join(f"• {e}" for e in res.errors)
                self.line.reply(reply_token, msg)
            elif command and command.split()[0].lower() == "/whoami":
                self.line.reply(reply_token, f"你的 userId：{user_id}")
            elif is_admin and command:
                self._run_command(command, reply_token, user_id, None)
            elif is_admin and text:
                self.line.reply(reply_token, "我只看得懂指令喔，輸入 /help 查看可以用的指令。")
            elif text:
                log.info("非管理者私訊（%s）", (user_id or "")[-6:])
                self.line.reply(reply_token, "這個帳號是系統回饋收集機器人，僅供管理者使用。\n"
                                             "管理者請輸入 /whoami 取得 userId，並設定到 ADMIN_USER_IDS。")
            return

        if not chat_id:
            return
        if not self.db.group(chat_id):
            name = self._group_name(chat_id) if src.get("groupId") else None
            self.db.upsert_group(chat_id, ts, name)

        # ---- 群組內管理者指令（不收進回饋）----
        if is_admin and command:
            self._run_command(command, reply_token, user_id, chat_id)
            return

        # ---- 設成「不收集」的群組：不記錄 ----
        if self.db.is_excluded(chat_id):
            return

        # ---- 默默收集 ----
        kind = msg.get("type")
        record = {
            "line_message_id": msg.get("id"),
            "group_id": chat_id,
            "user_id": user_id,
            "display_name": self._display_name(chat_id, user_id),
            "created_at": ts.isoformat(),
            "quoted_message_id": msg.get("quotedMessageId"),
        }
        if kind == "text":
            self.db.add_message(kind="text", text=text, **record)
        elif kind == "image":
            ref = None
            try:
                got = self.line.download_content(msg["id"])
                if got:
                    data, ctype = got
                    ext = {"image/png": "png", "image/gif": "gif", "image/webp": "webp"}.get(ctype, "jpg")
                    ref = self.files.save(f"images/{chat_id[-8:]}/{msg['id']}.{ext}", data, ctype)
            except Exception:  # noqa: BLE001  圖存不了也要留下這則訊息
                log.warning("儲存截圖失敗", exc_info=True)
            self.db.add_message(kind="image", image_ref=ref, **record)
        elif kind == "file":
            self.db.add_message(kind="file", text=msg.get("fileName"), **record)
        # 貼圖、影片、語音等不收

    def bot_profile(self) -> dict:
        """機器人在 LINE 上的名稱與頭像（快取 1 小時）。"""
        import time

        cached = getattr(self, "_profile", None)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        try:
            info = self.line.bot_info()
            prof = {"name": info.get("displayName") or "回饋收集機器人", "picture": info.get("pictureUrl"),
                    "basic_id": info.get("basicId")}
        except Exception:  # noqa: BLE001
            prof = {"name": "回饋收集機器人", "picture": None, "basic_id": None}
        self._profile = (time.time(), prof)
        return prof

    def _group_name(self, chat_id: str) -> str | None:
        try:
            return self.line.group_name(chat_id)
        except Exception:  # noqa: BLE001  取不到名稱不影響收集
            log.warning("取得群組名稱失敗", exc_info=True)
            return None

    def _report_link(self, name: str) -> str:
        if self.s.public_base_url:
            return f"{self.s.public_base_url}/admin/reports?name={name}"
        return f"後台「報告」→ {name}"

    def _display_name(self, chat_id: str, user_id: str | None) -> str | None:
        if not user_id:
            return None
        key = (chat_id, user_id)
        if key not in self._names:
            try:
                self._names[key] = self.line.member_name(chat_id, user_id) or ""
            except Exception:  # noqa: BLE001
                self._names[key] = ""
        return self._names[key] or None

    def _run_command(self, text: str, reply_token: str, user_id: str, group_id: str | None) -> None:
        cmd = text.split()[0].lower()
        ctx = commands.Ctx(db=self.db, line=self.line, now=self.now, group_id=group_id, systems=self.systems,
                           run_digest=self.job_digest, run_announce=self.job_announce, run_sync=self.job_sync,
                           run_analyze=self.job_analyze)
        if cmd in ("/analyze", "/sync") and not self.s.code_analysis:
            self.line.reply(reply_token, "目前是手動模式：David 不讀程式碼分析、也不自動配對 commit。\n"
                                         "問題請到後台整理；處理好後用 /resolve 編號 怎麼改的 | 在哪裡 標記完成。")
            return
        if cmd == "/analyze":
            iid = int(text.split()[1].lstrip("#")) if len(text.split()) > 1 and text.split()[1].lstrip("#").isdigit() else 0
            err = pipeline.request_analysis(self.db, iid, self.now()) if iid else "格式：/analyze 問題編號"
            if err:
                self.line.reply(reply_token, err)
                return
            self.line.reply(reply_token, f"⏳ 開始分析 #{iid}（會讀程式碼，約 1～3 分鐘），完成後私訊通知你…")
            self.start_job("analyze")
            return
        if cmd in LONG_JOBS:
            # 時間較長：先回覆，再執行，完成後推播給管理者
            self.line.reply(reply_token, "⏳ 處理中，完成後會私訊通知你…")
            _spawn(self._run_long, LONG_JOBS[cmd], ctx, text)
            return
        self.line.reply(reply_token, commands.handle(ctx, text))

    def _run_long(self, job: str, ctx, text: str) -> None:
        """長時間工作：在 Cloud Run 上改成呼叫自己的 /tasks/<job>。
        對 Cloud Run 來說那是一個進行中的請求，工作做完前不會把機器關掉；
        若單純在背景執行緒跑，閒置一段時間後機器可能被關閉，工作就中斷了。"""
        if self.self_url and self.s.task_token:
            try:
                r = httpx.post(f"{self.self_url}/tasks/{job}", headers={"x-task-token": self.s.task_token},
                               timeout=3600)
                if r.status_code == 200:
                    return
                log.warning("呼叫 /tasks/%s 失敗（HTTP %s），改在背景執行", job, r.status_code)
            except httpx.TimeoutException:
                return  # 伺服器端仍在執行，完成後會自己通知
            except Exception:  # noqa: BLE001
                log.exception("呼叫 /tasks/%s 失敗，改在背景執行", job)
        if text is None:
            {"digest": self.job_digest, "sync": self.job_sync, "announce": self.job_announce,
             "analyze": self.job_analyze}[job]()
        else:
            commands.handle(ctx, text)

    # ---------------- 排程 ----------------
    def start_scheduler(self) -> BackgroundScheduler:
        sched = BackgroundScheduler(timezone=self.tz)
        h, m = self.s.digest_at.split(":")
        sched.add_job(self.job_digest, CronTrigger(hour=int(h), minute=int(m), timezone=self.tz),
                      id="digest", misfire_grace_time=3600, coalesce=True)
        h, m = self.s.announce_at.split(":")
        sched.add_job(self.job_announce, CronTrigger(hour=int(h), minute=int(m), timezone=self.tz),
                      id="announce", misfire_grace_time=3600, coalesce=True)
        sched.start()
        log.info("scheduler started: digest %s, announce %s (%s)", self.s.digest_at, self.s.announce_at, self.tz)
        return sched


def create_app(bot: Bot | None = None, start_scheduler: bool = True) -> FastAPI:
    state: dict = {"bot": bot}

    def get_bot() -> Bot:
        if state["bot"] is None:
            state["bot"] = Bot()
        return state["bot"]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        b = get_bot()
        sched = b.start_scheduler() if (start_scheduler and b.s.enable_scheduler) else None
        yield
        if sched:
            sched.shutdown(wait=False)

    api = FastAPI(title="LINE Feedback Bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    api.include_router(build_router(get_bot))
    api.add_exception_handler(LoginRequired, login_redirect_handler)

    @api.middleware("http")
    async def no_cache_admin(request: Request, call_next):
        b = state.get("bot")
        if b is not None and not b.self_url and request.url.hostname and request.url.hostname.endswith(".run.app"):
            b.self_url = f"https://{request.url.hostname}"  # 記下自己的 Cloud Run 網址
        resp = await call_next(request)
        if request.url.path.startswith(("/admin", "/tasks")):
            # 不讓 Firebase Hosting／瀏覽器快取後台頁面
            resp.headers["Cache-Control"] = "private, no-store"
        return resp

    # Cloud Run 會攔截結尾是 z 的網址（例如 /healthz），所以主要用 /health
    @api.get("/health")
    @api.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @api.post("/tasks/{job}")
    def run_task(job: str, request: Request) -> dict:
        """給 Cloud Scheduler 呼叫的排程工作（需帶 X-Task-Token）。"""
        b = get_bot()
        token = request.headers.get("x-task-token", "")
        if not b.s.task_token or not secrets.compare_digest(token.encode(), b.s.task_token.encode()):
            raise HTTPException(status_code=403, detail="forbidden")
        jobs = {"digest": b.job_digest, "announce": b.job_announce, "sync": b.job_sync, "analyze": b.job_analyze}
        if job not in jobs:
            raise HTTPException(status_code=404, detail="unknown job")
        return {"ok": True, "summary": jobs[job]()}

    @api.post("/callback")
    async def callback(request: Request, background: BackgroundTasks) -> dict:
        b = get_bot()
        if not b.self_url and request.url.hostname and request.url.hostname.endswith(".run.app"):
            b.self_url = f"https://{request.url.hostname}"
        body = await request.body()
        if not verify_signature(b.s.line_channel_secret, body, request.headers.get("x-line-signature")):
            raise HTTPException(status_code=400, detail="invalid signature")
        payload = await request.json()
        for ev in payload.get("events", []):
            background.add_task(_safe_handle, b, ev)
        return {"ok": True}  # LINE 要求盡快回 200

    return api


def _spawn(fn, *args) -> None:
    """在背景執行（測試時可替換成同步執行）。"""
    threading.Thread(target=fn, args=args, daemon=True).start()


LONG_JOBS = {"/today": "digest", "/sync": "sync", "/announce": "announce"}


def _as_command(text: str) -> str:
    """把「/指令」或全形「／指令」整理成標準格式；不是指令就回傳空字串。"""
    t = (text or "").strip()
    if t.startswith("／"):
        t = "/" + t[1:]
    return t if t.startswith("/") and len(t) > 1 else ""


def _safe_handle(bot: Bot, ev: dict) -> None:
    try:
        bot.handle_event(ev)
    except Exception:  # noqa: BLE001
        log.exception("handle event failed: %s", ev.get("type"))


app = create_app()
