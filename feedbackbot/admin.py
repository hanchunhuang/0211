"""網頁後台：查看收集的訊息、各群組統計、問題分析、公告與報告。

登入：/admin/login 輸入密碼（.env 的 ADMIN_WEB_PASSWORD），以 cookie「__session」保持登入
（Firebase Hosting 只會轉送這個名稱的 cookie）。沒設密碼時後台關閉。請務必走 HTTPS。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import pipeline
from .db import CLOSED
from .pipeline import KIND_ZH, STATUS_ZH

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.globals.update(KIND_ZH=KIND_ZH, STATUS_ZH=STATUS_ZH)
PAGE_SIZE = 100
COOKIE = "__session"
SESSION_HOURS = 12


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%m/%d %H:%M")
    except ValueError:
        return iso


TEMPLATES.env.filters["t"] = _fmt_time
TEMPLATES.env.filters["fromjson"] = lambda s: json.loads(s) if s else None


class LoginRequired(Exception):
    """未登入：GET 轉到登入頁。"""

    def __init__(self, next_url: str):
        self.next_url = next_url


# ============================================================ 登入 cookie


def _secret(settings) -> bytes:
    base = settings.session_secret or ("pw:" + settings.admin_web_password)
    return hashlib.sha256(base.encode()).digest()


def make_session(settings) -> str:
    exp = int(time.time()) + SESSION_HOURS * 3600
    sig = hmac.new(_secret(settings), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_session(settings, value: str | None) -> bool:
    if not value or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    if not exp.isdigit() or int(exp) < time.time():
        return False
    good = hmac.new(_secret(settings), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(good, sig)


def _allowed_hosts(request: Request, settings) -> set[str]:
    hosts = {request.url.netloc}
    fwd = request.headers.get("x-forwarded-host")
    if fwd:
        hosts.add(fwd.split(",")[0].strip())
    if settings.public_base_url:
        hosts.add(urlparse(settings.public_base_url).netloc)
    return hosts


def _check_same_origin(request: Request, settings) -> None:
    """防止其他網站利用瀏覽器的登入狀態送出表單（CSRF）。"""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src or urlparse(src).netloc not in _allowed_hosts(request, settings):
        raise HTTPException(403, "來源不符")


def _is_https(request: Request, settings) -> bool:
    return (settings.public_base_url.startswith("https://") or request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").startswith("https"))


# ============================================================ 路由


def build_router(get_bot: Callable) -> APIRouter:
    def auth(request: Request):
        bot = get_bot()
        if not bot.s.admin_web_password:
            raise HTTPException(503, "後台未啟用：請在設定中填入 ADMIN_WEB_PASSWORD")
        if not valid_session(bot.s, request.cookies.get(COOKIE)):
            if request.method == "GET":
                raise LoginRequired(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
            raise HTTPException(401, "請先登入")
        if request.method == "POST":
            _check_same_origin(request, bot.s)
        return bot

    public = APIRouter(prefix="/admin")
    r = APIRouter(prefix="/admin", dependencies=[Depends(auth)])

    def render(request: Request, template: str, **ctx) -> HTMLResponse:
        systems = get_bot().systems
        return TEMPLATES.TemplateResponse(request, template, {
            "nav": template.split(".")[0], "sysname": systems.name, "sys_options": systems.options(), **ctx})

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------ 登入／登出
    @public.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/admin", error: str = ""):
        if not get_bot().s.admin_web_password:
            raise HTTPException(503, "後台未啟用：請在設定中填入 ADMIN_WEB_PASSWORD")
        return render(request, "login.html", next=_safe_next(next), error=error)

    @public.post("/login")
    def login(request: Request, password: str = Form(...), next: str = Form("/admin")):
        bot = get_bot()
        pw = bot.s.admin_web_password
        if not pw:
            raise HTTPException(503, "後台未啟用")
        _check_same_origin(request, bot.s)
        if not secrets.compare_digest(password.encode(), pw.encode()):
            time.sleep(1)  # 減緩暴力猜測
            return back(f"/admin/login?error=1&next={quote(_safe_next(next))}")
        resp = back(_safe_next(next))
        resp.set_cookie(COOKIE, make_session(bot.s), max_age=SESSION_HOURS * 3600, httponly=True,
                        secure=_is_https(request, bot.s), samesite="lax", path="/")
        return resp

    @public.post("/logout")
    def logout():
        resp = back("/admin/login")
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ------------------------------------------------------------ 總覽
    @r.get("", response_class=HTMLResponse)
    def overview(request: Request, group: str = "", days: int = 14):
        bot = get_bot()
        db = bot.db
        days = days if days in (7, 14, 30, 90) else 14
        now = bot.now()
        data = Snapshot(db)
        return render(request, "overview.html", groups=group_stats(data, db, now), group=group, days=days,
                      tiles=overview_tiles(data, db, now, group), series=daily_feedback(data, db, now, days, group),
                      kinds=kind_breakdown(data, group), group_label=db.group_label(group) if group else "",
                      by_system=system_breakdown(data, bot.systems, group))

    # ------------------------------------------------------------ 負責單位
    @r.get("/units", response_class=HTMLResponse)
    def units(request: Request):
        bot = get_bot()
        db = bot.db
        rows = group_stats(Snapshot(db), db, bot.now())
        active = [g for g in rows if g.get("active") and not g.get("excluded")]
        excluded = [g for g in rows if g.get("active") and g.get("excluded")]
        left = [g for g in rows if not g.get("active")]
        unset = [g for g in active if not g.get("center_set")]
        return render(request, "units.html", profile=bot.bot_profile(), active=active, left=left, unset=unset,
                      excluded=excluded)

    # ------------------------------------------------------------ 群組
    @r.get("/groups/{gid}", response_class=HTMLResponse)
    def group_detail(request: Request, gid: str, page: int = 1):
        bot = get_bot()
        db = bot.db
        g = db.group(gid)
        if not g:
            raise HTTPException(404)
        data = Snapshot(db)
        stats = next((x for x in group_stats(data, db, bot.now()) if x["id"] == gid), None)
        my_fb = [f for f in data.feedback if f["group_id"] == gid]
        counts = Counter(f["issue_id"] for f in my_fb if f.get("issue_id"))
        issues = sorted((dict(i, n=counts[i["id"]]) for i in data.issues if i["id"] in counts),
                        key=lambda i: (i["status"] in CLOSED, -i["id"]))
        msgs, total = db.messages_page(gid, "", page, PAGE_SIZE)
        _mark_used(msgs, data.feedback)
        return render(request, "group.html", g=g, stats=stats, issues=issues, feedback=list(reversed(my_fb))[:100],
                      msgs=msgs, page=page, pages=max(1, -(-total // PAGE_SIZE)), label=db.group_label(gid),
                      shared=lambda iid: len(data.issue_groups(iid)))

    @r.post("/groups/{gid}/systems")
    async def set_systems(gid: str, request: Request):
        bot = get_bot()
        form = await request.form()
        chosen = [x for x in form.getlist("systems") if bot.systems.valid(x)]
        bot.db.set_group_systems(gid, chosen)
        return back(f"/admin/groups/{gid}")

    @r.post("/groups/{gid}/exclude")
    def set_exclude(gid: str, action: str = Form(...)):
        get_bot().db.set_group_excluded(gid, action == "exclude")
        return back(f"/admin/groups/{gid}")

    @r.post("/groups/{gid}/center")
    def set_center(gid: str, center: str = Form(...)):
        get_bot().db.set_group_company(gid, center.strip())
        return back(f"/admin/groups/{gid}")

    # ------------------------------------------------------------ 訊息
    @r.get("/messages", response_class=HTMLResponse)
    def messages(request: Request, group: str = "", q: str = "", page: int = 1):
        db = get_bot().db
        msgs, total = db.messages_page(group, q.strip(), page, PAGE_SIZE)
        _mark_used(msgs, db.all_feedback())
        return render(request, "messages.html", msgs=msgs, group=group, q=q, page=page,
                      pages=max(1, -(-total // PAGE_SIZE)), total=total, groups=db.groups(), label=db.group_label)

    @r.get("/images/{mid}")
    def image(mid: int):
        bot = get_bot()
        m = bot.db.message(mid)
        got = bot.files.read(m.get("image_ref")) if m and m.get("image_ref") else None
        if not got:
            raise HTTPException(404)
        data, ctype = got
        return Response(data, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})

    # ------------------------------------------------------------ 問題
    @r.get("/issues", response_class=HTMLResponse)
    def issues(request: Request, status: str = "open", kind: str = "", scope: str = "", group: str = "",
               system: str = ""):
        db = get_bot().db
        rows = issue_list(Snapshot(db), db, status, kind, scope, group, system)
        return render(request, "issues.html", rows=rows, status=status, kind=kind, scope=scope, group=group, system=system,
                      groups=db.groups(), label=db.group_label)

    @r.get("/issues/{iid}", response_class=HTMLResponse)
    def issue_detail(request: Request, iid: int):
        db = get_bot().db
        i = db.issue(iid)
        if not i:
            raise HTTPException(404)
        fb = db.issue_feedback(iid)
        by_group: dict[str, list] = {}
        for f in fb:
            by_group.setdefault(f["group_id"], []).append(f)
        msg_ids = [m for f in fb for m in json.loads(f["message_ids"])]
        anns = [a for a in db.announcements() if iid in json.loads(a["issue_ids"])]
        return render(request, "issue.html", i=i, by_group=by_group, source=db.messages_by_ids(msg_ids), anns=anns,
                      analysis=json.loads(i["analysis"]) if i.get("analysis") else None,
                      commits=json.loads(i.get("fix_commits") or "[]"), label=db.group_label)

    @r.post("/issues/{iid}/system")
    def issue_system(iid: int, system: str = Form(...)):
        bot = get_bot()
        if not bot.systems.valid(system):
            raise HTTPException(400, "系統代號不正確")
        if not pipeline.set_issue_system(bot.db, iid, system, bot.now()):
            raise HTTPException(404)
        return back(f"/admin/issues/{iid}")

    @r.post("/issues/{iid}/resolve")
    def issue_resolve(iid: int, note: str = Form(...), location: str = Form("")):
        bot = get_bot()
        pipeline.resolve_issue(bot.db, iid, note.strip(), location.strip(), bot.now())
        return back(f"/admin/issues/{iid}")

    @r.post("/issues/{iid}/status")
    def issue_status(iid: int, action: str = Form(...), note: str = Form("")):
        bot = get_bot()
        if not bot.db.issue(iid):
            raise HTTPException(404)
        if action == "reopen":
            pipeline.reopen_issue(bot.db, iid, bot.now())
        elif action == "start":
            bot.db.update_issue(iid, bot.now(), status="in_progress")
        elif action == "wontfix":
            pipeline.resolve_issue(bot.db, iid, note.strip(), "", bot.now(), status="wontfix")
        return back(f"/admin/issues/{iid}")

    # ------------------------------------------------------------ 公告
    @r.get("/announcements", response_class=HTMLResponse)
    def announcements(request: Request):
        db = get_bot().db
        return render(request, "announcements.html", rows=db.announcements()[:200], label=db.group_label)

    @r.post("/announcements/{aid}/{action}")
    def announcement_action(aid: int, action: str):
        bot = get_bot()
        if action == "approve":
            pipeline.send_announcements(bot.db, bot.line, bot.now(), [aid])
        elif action == "skip":
            bot.db.set_announcement_status(aid, "skipped", bot.now())
        return back("/admin/announcements")

    # ------------------------------------------------------------ 報告
    @r.get("/reports", response_class=HTMLResponse)
    def reports(request: Request, name: str = ""):
        db = get_bot().db
        files = db.report_names()
        content = ""
        if name:
            rep = db.report(name) if name in files else None
            if not rep:
                raise HTTPException(404)
            content = rep["content"]
        return render(request, "reports.html", files=files, name=name, content=content)

    router = APIRouter()
    router.include_router(public)
    router.include_router(r)
    return router


def _safe_next(url: str) -> str:
    """登入後只能導回本站的後台頁面。"""
    return url if url.startswith("/admin") and not url.startswith("//") else "/admin"


async def login_redirect_handler(request: Request, exc: LoginRequired):
    return RedirectResponse(f"/admin/login?next={quote(exc.next_url)}", status_code=303)


# ====================================================================== 統計（在記憶體中計算；回饋與問題數量不大）


class Snapshot:
    """一次載入群組、回饋、問題，避免每個統計重複查資料庫。"""

    def __init__(self, db):
        self.groups = db.groups()
        self.feedback = db.all_feedback()
        self.issues = db.all_issues()
        self._issue_groups: dict[int, set[str]] = {}
        for f in self.feedback:
            if f.get("issue_id"):
                self._issue_groups.setdefault(f["issue_id"], set()).add(f["group_id"])

    def issue_groups(self, iid: int) -> list[str]:
        return sorted(self._issue_groups.get(iid, set()))


def group_stats(data: Snapshot, db, now: datetime) -> list[dict]:
    week = (now - timedelta(days=7)).isoformat()
    issue_status = {i["id"]: i["status"] for i in data.issues}
    out = []
    for g in sorted(data.groups, key=lambda g: (not g.get("active"), g.get("joined_at") or "")):
        fb = [f for f in data.feedback if f["group_id"] == g["id"]]
        my_issues = {f["issue_id"] for f in fb if f.get("issue_id")}
        kinds = {k: 0 for k in KIND_ZH}
        for f in fb:
            kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
        out.append(dict(
            g, label=g.get("company") or g.get("name") or g["id"][-6:],
            msgs=g.get("msg_count") or 0, msgs7=db.count_messages(since=week, group_id=g["id"]),
            last_msg=g.get("last_msg_at"), fb=len(fb), kinds=kinds,
            open_issues=sum(1 for i in my_issues if issue_status.get(i) not in CLOSED),
            resolved=sum(1 for i in my_issues if issue_status.get(i) == "resolved"),
        ))
    return out


def overview_tiles(data: Snapshot, db, now: datetime, group: str = "") -> dict:
    week = (now - timedelta(days=7)).isoformat()
    week_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    open_issues = issue_list(data, db, "open", "", "", group)
    common = sum(1 for i in open_issues if i["n_groups"] >= 2)
    unannounced = 0
    for g in ([group] if group else [x["id"] for x in data.groups if x.get("active")]):
        done = db.announced_issue_ids(g)
        mine = {f["issue_id"] for f in data.feedback if f["group_id"] == g and f.get("issue_id")}
        unannounced += sum(1 for i in data.issues if i["id"] in mine and i["status"] == "resolved" and i["id"] not in done)
    fb = [f for f in data.feedback if not group or f["group_id"] == group]
    return {
        "groups": sum(1 for g in data.groups if g.get("active") and not g.get("excluded")),
        "msgs7": db.count_messages(since=week, group_id=group or None),
        "fb7": sum(1 for f in fb if f["digest_date"] >= week_date),
        "open": len(open_issues), "common": common, "single": len(open_issues) - common,
        "unannounced": unannounced,
        "pending_msgs": db.count_messages(group_id=group or None, unprocessed=True),
    }


def daily_feedback(data: Snapshot, db, now: datetime, days: int, group: str = "") -> list[dict]:
    start = (now - timedelta(days=days - 1)).date()
    counts = Counter(f["digest_date"] for f in data.feedback
                     if f["digest_date"] >= start.isoformat() and (not group or f["group_id"] == group))
    msgs = Counter(t[:10] for t in db.message_times(start.isoformat(), group or None))
    out = []
    for k in range(days):
        d = (start + timedelta(days=k)).isoformat()
        out.append({"date": d, "label": d[5:].replace("-", "/"), "n": counts.get(d, 0), "msgs": msgs.get(d, 0)})
    return out


def system_breakdown(data: Snapshot, systems, group: str = "") -> list[dict]:
    """各系統的回饋數與未完成問題數。"""
    fb = [f for f in data.feedback if not group or f["group_id"] == group]
    mine = {f["issue_id"] for f in fb if f.get("issue_id")}
    out = []
    for sid, name in systems.options() + [("unknown", "未分類")]:
        n_fb = sum(1 for f in fb if (f.get("system_id") or "unknown") == sid)
        issues = [i for i in data.issues if (i.get("system_id") or "unknown") == sid and (not group or i["id"] in mine)]
        open_n = sum(1 for i in issues if i["status"] not in CLOSED)
        common = sum(1 for i in issues if i["status"] not in CLOSED and len(data.issue_groups(i["id"])) >= 2)
        if sid == "unknown" and not n_fb and not issues:
            continue
        out.append({"id": sid, "name": name, "feedback": n_fb, "open": open_n, "common": common,
                    "resolved": sum(1 for i in issues if i["status"] == "resolved")})
    return out


def kind_breakdown(data: Snapshot, group: str = "") -> list[dict]:
    got = Counter(f["kind"] for f in data.feedback if not group or f["group_id"] == group)
    total = sum(got.values()) or 1
    return [{"kind": k, "label": v, "n": got.get(k, 0), "pct": round(got.get(k, 0) * 100 / total)}
            for k, v in KIND_ZH.items()]


def issue_list(data: Snapshot, db, status: str, kind: str, scope: str, group: str, system: str = "") -> list[dict]:
    rows = []
    for i in data.issues:
        if system and (i.get("system_id") or "unknown") != system:
            continue
        if status == "open":
            if i["status"] in CLOSED:
                continue
        elif status in STATUS_ZH and i["status"] != status:
            continue
        if kind and i["kind"] != kind:
            continue
        gids = data.issue_groups(i["id"])
        if group and group not in gids:
            continue
        if scope == "common" and len(gids) < 2 or scope == "single" and len(gids) >= 2:
            continue
        fb = [f for f in data.feedback if f.get("issue_id") == i["id"]]
        rows.append(dict(i, n_feedback=len(fb), n_groups=len(gids), groups=[db.group_label(g) for g in gids],
                         last_date=max((f["digest_date"] for f in fb), default=None)))
    rows.sort(key=lambda i: (i.get("priority") or "P9", -i["n_groups"], -i["id"]))
    return rows


def _mark_used(msgs: list[dict], feedback: list[dict]) -> None:
    used: dict[int, int] = {}
    for f in feedback:
        for m in json.loads(f["message_ids"]):
            used[m] = f["id"]
    for m in msgs:
        m["feedback_id"] = used.get(m["id"])
