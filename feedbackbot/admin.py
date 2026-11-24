"""網頁後台：查看收集的訊息、各群組統計、問題分析、公告與報告。

登入：HTTP Basic（.env 的 ADMIN_WEB_USER / ADMIN_WEB_PASSWORD）。沒設密碼時後台關閉。
請務必放在 HTTPS 後面。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from . import pipeline
from .pipeline import KIND_ZH, STATUS_ZH

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.globals.update(KIND_ZH=KIND_ZH, STATUS_ZH=STATUS_ZH)
PAGE_SIZE = 100


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%m/%d %H:%M")
    except ValueError:
        return iso


TEMPLATES.env.filters["t"] = _fmt_time
TEMPLATES.env.filters["fromjson"] = lambda s: json.loads(s) if s else None

basic = HTTPBasic(realm="feedback-admin")


def build_router(get_bot: Callable) -> APIRouter:
    def auth(request: Request, cred: HTTPBasicCredentials = Depends(basic)):
        bot = get_bot()
        pw = bot.s.admin_web_password
        if not pw:
            raise HTTPException(503, "後台未啟用：請在 .env 設定 ADMIN_WEB_PASSWORD")
        ok = secrets.compare_digest(cred.username.encode(), bot.s.admin_web_user.encode()) and \
            secrets.compare_digest(cred.password.encode(), pw.encode())
        if not ok:
            raise HTTPException(401, "帳號或密碼錯誤", headers={"WWW-Authenticate": 'Basic realm="feedback-admin"'})
        if request.method == "POST":
            _check_same_origin(request)
        return bot

    r = APIRouter(prefix="/admin", dependencies=[Depends(auth)])

    def render(request: Request, template: str, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, template, {"nav": template.split(".")[0], **ctx})

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------ 總覽
    @r.get("", response_class=HTMLResponse)
    def overview(request: Request, group: str = "", days: int = 14):
        bot = get_bot()
        db = bot.db
        days = days if days in (7, 14, 30, 90) else 14
        now = bot.now()
        groups = group_stats(db, now)
        return render(request, "overview.html", groups=groups, group=group, days=days,
                      tiles=overview_tiles(db, now, group), series=daily_feedback(db, now, days, group),
                      kinds=kind_breakdown(db, group), group_label=db.group_label(group) if group else "")

    # ------------------------------------------------------------ 群組
    @r.get("/groups/{gid}", response_class=HTMLResponse)
    def group_detail(request: Request, gid: str, page: int = 1):
        db = get_bot().db
        g = db.one("SELECT * FROM groups WHERE id=?", (gid,))
        if not g:
            raise HTTPException(404)
        stats = next((x for x in group_stats(db, get_bot().now()) if x["id"] == gid), None)
        issues = db.q("""SELECT i.*, COUNT(f.id) AS n FROM issues i JOIN feedback f ON f.issue_id=i.id
                         WHERE f.group_id=? GROUP BY i.id ORDER BY i.status IN ('resolved','wontfix'), i.id DESC""",
                      (gid,))
        feedback = db.q("SELECT * FROM feedback WHERE group_id=? ORDER BY id DESC LIMIT 100", (gid,))
        msgs, total = messages_page(db, gid, "", page)
        return render(request, "group.html", g=g, stats=stats, issues=issues, feedback=feedback, msgs=msgs,
                      page=page, pages=max(1, -(-total // PAGE_SIZE)), label=db.group_label(gid),
                      shared=lambda iid: len(db.issue_groups(iid)))

    @r.post("/groups/{gid}/center")
    def set_center(gid: str, center: str = Form(...)):
        get_bot().db.set_group_company(gid, center.strip())
        return back(f"/admin/groups/{gid}")

    # ------------------------------------------------------------ 訊息
    @r.get("/messages", response_class=HTMLResponse)
    def messages(request: Request, group: str = "", q: str = "", page: int = 1):
        db = get_bot().db
        msgs, total = messages_page(db, group, q, page)
        return render(request, "messages.html", msgs=msgs, group=group, q=q, page=page,
                      pages=max(1, -(-total // PAGE_SIZE)), total=total,
                      groups=db.q("SELECT * FROM groups ORDER BY joined_at"), label=db.group_label)

    @r.get("/images/{mid}")
    def image(mid: int):
        bot = get_bot()
        row = bot.db.one("SELECT image_path FROM messages WHERE id=?", (mid,))
        if not row or not row["image_path"]:
            raise HTTPException(404)
        p = Path(row["image_path"]).resolve()
        if bot.s.images_dir.resolve() not in p.parents or not p.is_file():
            raise HTTPException(404)
        return FileResponse(p)

    # ------------------------------------------------------------ 問題
    @r.get("/issues", response_class=HTMLResponse)
    def issues(request: Request, status: str = "open", kind: str = "", scope: str = "", group: str = ""):
        db = get_bot().db
        rows = issue_list(db, status, kind, scope, group)
        return render(request, "issues.html", rows=rows, status=status, kind=kind, scope=scope, group=group,
                      groups=db.q("SELECT * FROM groups ORDER BY joined_at"), label=db.group_label)

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
        source = db.q(f"SELECT * FROM messages WHERE id IN ({','.join('?' * len(msg_ids))}) ORDER BY id",
                      tuple(msg_ids)) if msg_ids else []
        anns = [a for a in db.q("SELECT * FROM announcements ORDER BY id DESC") if iid in json.loads(a["issue_ids"])]
        return render(request, "issue.html", i=i, by_group=by_group, source=source, anns=anns,
                      analysis=json.loads(i["analysis"]) if i["analysis"] else None,
                      commits=json.loads(i["fix_commits"] or "[]"), label=db.group_label)

    @r.post("/issues/{iid}/resolve")
    def issue_resolve(iid: int, note: str = Form(...), location: str = Form("")):
        bot = get_bot()
        pipeline.resolve_issue(bot.db, iid, note.strip(), location.strip(), bot.now())
        return back(f"/admin/issues/{iid}")

    @r.post("/issues/{iid}/status")
    def issue_status(iid: int, action: str = Form(...), note: str = Form("")):
        bot = get_bot()
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
        rows = db.q("SELECT * FROM announcements ORDER BY id DESC LIMIT 200")
        return render(request, "announcements.html", rows=rows, label=db.group_label)

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
        d = get_bot().s.reports_dir
        files = sorted((p.name for p in d.glob("*.md")), reverse=True) if d.exists() else []
        content = ""
        if name:
            if name not in files:
                raise HTTPException(404)
            content = (d / name).read_text(encoding="utf-8")
        return render(request, "reports.html", files=files, name=name, content=content)

    return r


def _check_same_origin(request: Request) -> None:
    """防止其他網站利用瀏覽器記住的登入送出表單（CSRF）。"""
    src = request.headers.get("origin") or request.headers.get("referer")
    if not src or urlparse(src).netloc != request.url.netloc:
        raise HTTPException(403, "來源不符")


# ====================================================================== 查詢


def group_stats(db, now: datetime) -> list[dict]:
    week = (now - timedelta(days=7)).isoformat()
    rows = db.q(
        """SELECT g.*,
             (SELECT COUNT(*) FROM messages m WHERE m.group_id=g.id) AS msgs,
             (SELECT COUNT(*) FROM messages m WHERE m.group_id=g.id AND m.created_at>=?) AS msgs7,
             (SELECT MAX(created_at) FROM messages m WHERE m.group_id=g.id) AS last_msg,
             (SELECT COUNT(*) FROM feedback f WHERE f.group_id=g.id) AS fb,
             (SELECT COUNT(DISTINCT f.issue_id) FROM feedback f JOIN issues i ON i.id=f.issue_id
                WHERE f.group_id=g.id AND i.status NOT IN ('resolved','wontfix')) AS open_issues,
             (SELECT COUNT(DISTINCT f.issue_id) FROM feedback f JOIN issues i ON i.id=f.issue_id
                WHERE f.group_id=g.id AND i.status='resolved') AS resolved
           FROM groups g ORDER BY g.active DESC, g.joined_at""", (week,))
    out = []
    for r in rows:
        d = dict(r)
        d["label"] = r["company"] or r["name"] or r["id"][-6:]
        kinds = {k: 0 for k in KIND_ZH}
        for k in db.q("SELECT kind, COUNT(*) n FROM feedback WHERE group_id=? GROUP BY kind", (r["id"],)):
            kinds[k["kind"]] = k["n"]
        d["kinds"] = kinds
        out.append(d)
    return out


def overview_tiles(db, now: datetime, group: str = "") -> dict:
    week = (now - timedelta(days=7)).isoformat()
    week_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    gfilter, gp = ("AND group_id=?", (group,)) if group else ("", ())
    open_issues = issue_list(db, "open", "", "", group)
    common = sum(1 for i in open_issues if i["n_groups"] >= 2)
    unannounced = 0
    for g in ([group] if group else [x["id"] for x in db.q("SELECT id FROM groups WHERE active=1")]):
        done = db.announced_issue_ids(g)
        unannounced += sum(1 for i in db.q(
            """SELECT DISTINCT i.id FROM issues i JOIN feedback f ON f.issue_id=i.id
               WHERE f.group_id=? AND i.status='resolved'""", (g,)) if i["id"] not in done)
    return {
        "groups": db.one("SELECT COUNT(*) n FROM groups WHERE active=1")["n"],
        "msgs7": db.one(f"SELECT COUNT(*) n FROM messages WHERE created_at>=? {gfilter}", (week, *gp))["n"],
        "fb7": db.one(f"SELECT COUNT(*) n FROM feedback WHERE digest_date>=? {gfilter}", (week_date, *gp))["n"],
        "open": len(open_issues), "common": common, "single": len(open_issues) - common,
        "unannounced": unannounced,
        "pending_msgs": db.one(f"SELECT COUNT(*) n FROM messages WHERE processed=0 {gfilter}", gp)["n"],
    }


def daily_feedback(db, now: datetime, days: int, group: str = "") -> list[dict]:
    start = (now - timedelta(days=days - 1)).date()
    gfilter, gp = ("AND group_id=?", (group,)) if group else ("", ())
    counts = {r["d"]: r["n"] for r in db.q(
        f"SELECT digest_date d, COUNT(*) n FROM feedback WHERE digest_date>=? {gfilter} GROUP BY digest_date",
        (start.isoformat(), *gp))}
    msgs = {r["d"]: r["n"] for r in db.q(
        f"SELECT substr(created_at,1,10) d, COUNT(*) n FROM messages WHERE created_at>=? {gfilter} GROUP BY d",
        (start.isoformat(), *gp))}
    out = []
    for k in range(days):
        d = (start + timedelta(days=k)).isoformat()
        out.append({"date": d, "label": d[5:].replace("-", "/"), "n": counts.get(d, 0), "msgs": msgs.get(d, 0)})
    return out


def kind_breakdown(db, group: str = "") -> list[dict]:
    gfilter, gp = ("WHERE group_id=?", (group,)) if group else ("", ())
    got = {r["kind"]: r["n"] for r in db.q(f"SELECT kind, COUNT(*) n FROM feedback {gfilter} GROUP BY kind", gp)}
    total = sum(got.values()) or 1
    return [{"kind": k, "label": v, "n": got.get(k, 0), "pct": round(got.get(k, 0) * 100 / total)}
            for k, v in KIND_ZH.items()]


def issue_list(db, status: str, kind: str, scope: str, group: str) -> list[dict]:
    where, params = [], []
    if status == "open":
        where.append("i.status NOT IN ('resolved','wontfix')")
    elif status in STATUS_ZH:
        where.append("i.status=?")
        params.append(status)
    if kind:
        where.append("i.kind=?")
        params.append(kind)
    if group:
        where.append("i.id IN (SELECT issue_id FROM feedback WHERE group_id=?)")
        params.append(group)
    sql = f"""SELECT i.*, COUNT(f.id) AS n_feedback, COUNT(DISTINCT f.group_id) AS n_groups,
                     MAX(f.digest_date) AS last_date
              FROM issues i LEFT JOIN feedback f ON f.issue_id=i.id
              {'WHERE ' + ' AND '.join(where) if where else ''}
              GROUP BY i.id ORDER BY COALESCE(i.priority,'P9'), n_groups DESC, i.id DESC"""
    rows = [dict(r) for r in db.q(sql, tuple(params))]
    if scope == "common":
        rows = [r for r in rows if r["n_groups"] >= 2]
    elif scope == "single":
        rows = [r for r in rows if r["n_groups"] < 2]
    for r in rows:
        r["groups"] = [db.group_label(g) for g in db.issue_groups(r["id"])]
    return rows


def messages_page(db, group: str, q: str, page: int) -> tuple[list[dict], int]:
    where, params = [], []
    if group:
        where.append("group_id=?")
        params.append(group)
    if q:
        where.append("(text LIKE ? OR display_name LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    w = ("WHERE " + " AND ".join(where)) if where else ""
    total = db.one(f"SELECT COUNT(*) n FROM messages {w}", tuple(params))["n"]
    page = max(1, page)
    rows = [dict(r) for r in db.q(f"SELECT * FROM messages {w} ORDER BY id DESC LIMIT ? OFFSET ?",
                                  (*params, PAGE_SIZE, (page - 1) * PAGE_SIZE))]
    used: dict[int, int] = {}
    for f in db.q("SELECT id, message_ids FROM feedback"):
        for m in json.loads(f["message_ids"]):
            used[m] = f["id"]
    for r in rows:
        r["feedback_id"] = used.get(r["id"])
    return rows, total
