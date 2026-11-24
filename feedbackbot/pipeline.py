"""每日循環：
1. digest()               每晚：萃取當天回饋 → 跨中心歸類（共同/單一）→ 查程式碼分析修改方式 → 產生日報
2. sync_commits()         讀取系統程式庫的新 commit，自動配對到已回報的問題並標記完成
   resolve_issue()        （或由管理者手動標記：怎麼改、改在哪裡）
3. prepare_announcements() 隔天早上：成效評估報告 + 為每個群組產生「已修正」公告草稿
4. send_announcements()   推播到群組（自動或經管理者 /approve）
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Settings
from .db import DB
from .llm import LLM
from .repo import CodeRepo

log = logging.getLogger(__name__)
RUN_LOCK = threading.Lock()

KIND_ZH = {"bug": "錯誤", "feature": "新需求", "usability": "操作不便", "question": "使用疑問"}
STATUS_ZH = {"open": "待分析", "analyzed": "已分析", "in_progress": "修改中", "resolved": "已完成", "wontfix": "不處理"}


def now_in(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


# ====================================================================== digest


@dataclass
class DigestResult:
    date: str
    new_feedback: int = 0
    touched_issue_ids: list[int] = field(default_factory=list)
    report_path: Path | None = None
    admin_summary: str = ""


def _transcript(db: DB, rows: list, staff_ids: list[str] | None = None) -> tuple[str, list[tuple[int, Path]]]:
    by_line_id = {r["line_message_id"]: r["id"] for r in rows if r["line_message_id"]}
    lines: list[str] = []
    images: list[tuple[int, Path]] = []
    for r in rows:
        t = datetime.fromisoformat(r["created_at"]).strftime("%m/%d %H:%M")
        name = r["display_name"] or "使用者"
        if staff_ids and r["user_id"] in staff_ids:
            name += "（客服）"
        quote = ""
        if r["quoted_message_id"]:
            q = by_line_id.get(r["quoted_message_id"])
            quote = f"（回覆 #{q}）" if q else "（回覆較早的訊息）"
        if r["kind"] == "text":
            body = r["text"] or ""
        elif r["kind"] == "image":
            body = "[傳送了一張截圖]"
            if r["image_path"] and Path(r["image_path"]).exists():
                images.append((r["id"], Path(r["image_path"])))
        else:
            body = f"[{r['kind']}]"
        lines.append(f"#{r['id']} [{t}] {name}{quote}: {body}")
    return "\n".join(lines), images


def digest(db: DB, llm: LLM, settings: Settings, now: datetime | None = None,
           repo: CodeRepo | None = None) -> DigestResult:
    now = now or now_in(settings)
    day = now.strftime("%Y-%m-%d")
    res = DigestResult(date=day)
    with RUN_LOCK:
        # ---- 1. 每個群組各自萃取回饋 ----
        for group_id, rows in db.unprocessed_messages().items():
            company = db.group_label(group_id)
            transcript, images = _transcript(db, rows, settings.staff_user_ids)
            valid_ids = {r["id"] for r in rows}
            if any(r["kind"] in ("text", "image") for r in rows):
                extraction = llm.extract(company, transcript, images[: settings.max_images_per_group])
                for it in extraction.items:
                    ids = [i for i in it.message_ids if i in valid_ids]
                    db.add_feedback(group_id=group_id, message_ids=ids, reporter=it.reporter, kind=it.kind,
                                    title=it.title, detail=it.detail, severity=it.severity,
                                    digest_date=day, now=now)
                    res.new_feedback += 1
            db.mark_processed([r["id"] for r in rows])

        # ---- 2. 跨群組歸類（找出共同問題）----
        new_fb = db.unlinked_feedback()
        touched: set[int] = set()
        if new_fb:
            open_issues = [
                {"id": i["id"], "title": i["title"], "kind": i["kind"], "summary": i["summary"],
                 "module": i["module"], "companies": [db.group_label(g) for g in db.issue_groups(i["id"])]}
                for i in db.open_issues()
            ]
            fb_payload = [
                {"feedback_id": f["id"], "company": db.group_label(f["group_id"]), "kind": f["kind"],
                 "title": f["title"], "detail": f["detail"]}
                for f in new_fb
            ]
            clustering = llm.cluster(open_issues, fb_payload)
            open_ids = {i["id"] for i in open_issues}
            key_to_issue: dict[str, int] = {}
            new_by_key = {n.key: n for n in clustering.new_issues}
            assigned: set[int] = set()
            fb_by_id = {f["id"]: f for f in new_fb}
            for a in clustering.assignments:
                if a.feedback_id not in fb_by_id or a.feedback_id in assigned:
                    continue
                if a.issue_id in open_ids:
                    issue_id = a.issue_id
                elif a.new_issue_key in new_by_key:
                    if a.new_issue_key not in key_to_issue:
                        n = new_by_key[a.new_issue_key]
                        key_to_issue[a.new_issue_key] = db.create_issue(n.title, n.kind, n.summary, n.module, now)
                    issue_id = key_to_issue[a.new_issue_key]
                else:
                    continue
                db.link_feedback(a.feedback_id, issue_id)
                assigned.add(a.feedback_id)
                touched.add(issue_id)
            # 模型漏掉的回饋：自成一個新問題，確保不遺失
            for fid, f in fb_by_id.items():
                if fid not in assigned:
                    issue_id = db.create_issue(f["title"], f["kind"], f["detail"], None, now)
                    db.link_feedback(fid, issue_id)
                    touched.add(issue_id)

        # ---- 3. 對今天有變動的問題做修改分析 ----
        if touched and repo is not None and repo.exists() and settings.repo_auto_update:
            _safe_update(repo)
        system_context = settings.system_context(repo)
        for issue_id in sorted(touched):
            issue = db.issue(issue_id)
            if issue["status"] in ("resolved", "wontfix", "in_progress"):
                continue
            feedback = _feedback_payload(db, issue_id)
            analysis = llm.analyze(
                {"id": issue_id, "title": issue["title"], "kind": issue["kind"], "summary": issue["summary"],
                 "module": issue["module"]},
                feedback, system_context, repo=repo)
            db.update_issue(issue_id, now, analysis=analysis.model_dump(), priority=analysis.priority,
                            status="analyzed")
        res.touched_issue_ids = sorted(touched)

        # ---- 4. 日報 ----
        res.report_path = _write_digest_report(db, settings, res, now)
        res.admin_summary = _admin_digest_summary(db, res)
        return res


def _feedback_payload(db: DB, issue_id: int) -> list[dict]:
    return [
        {"company": db.group_label(f["group_id"]), "reporter": f["reporter"], "kind": f["kind"],
         "title": f["title"], "detail": f["detail"], "severity": f["severity"], "date": f["digest_date"]}
        for f in db.issue_feedback(issue_id)
    ]


def _is_common(db: DB, issue_id: int) -> bool:
    return len(db.issue_groups(issue_id)) >= 2


def _issue_md(db: DB, issue_id: int, today: str) -> str:
    i = db.issue(issue_id)
    companies = [db.group_label(g) for g in db.issue_groups(issue_id)]
    out = [f"### #{i['id']} {i['title']}",
           f"- 類型：{KIND_ZH.get(i['kind'], i['kind'])}　優先級：{i['priority'] or '-'}　狀態：{STATUS_ZH.get(i['status'], i['status'])}",
           f"- 模組：{i['module'] or '-'}",
           f"- 回報中心：{'、'.join(companies)}",
           f"- 問題摘要：{i['summary']}",
           "- 使用者原話："]
    for f in db.issue_feedback(issue_id):
        new = "🆕 " if f["digest_date"] == today else ""
        out.append(f"  - {new}[{db.group_label(f['group_id'])}] {f['reporter'] or ''}：{f['detail']}")
    if i["analysis"]:
        a = json.loads(i["analysis"])
        out += [
            "- **分析**",
            f"  - 理解：{a['understanding']}",
            f"  - 可能原因：{a['root_cause']}",
            f"  - 影響範圍：{a['affected_area']}　工作量：{a['effort']}",
            "  - 建議修改：",
            *[f"    {n}. {s}" for n, s in enumerate(a["proposed_changes"], 1)],
        ]
        if a.get("risks"):
            out.append("  - 風險：" + "；".join(a["risks"]))
        if a.get("questions_to_confirm"):
            out.append("  - 需向使用者確認：" + "；".join(a["questions_to_confirm"]))
        if a.get("acceptance_checks"):
            out.append("  - 驗收方式：" + "；".join(a["acceptance_checks"]))
        if a.get("user_path"):
            out.append(f"  - 使用者操作位置：{a['user_path']}")
        if a.get("files_to_change"):
            out.append("  - 建議修改的檔案：")
            out += [f"    - `{f['path']}`：{f['change']}" for f in a["files_to_change"]]
    if i["resolution_note"]:
        out.append(f"- 處理：{i['resolution_note']}｜{i['resolution_location'] or ''}")
    return "\n".join(out)


def _write_digest_report(db: DB, settings: Settings, res: DigestResult, now: datetime) -> Path:
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    common = [i for i in res.touched_issue_ids if _is_common(db, i)]
    single = [i for i in res.touched_issue_ids if not _is_common(db, i)]
    backlog = db.open_issues()
    lines = [
        f"# 回饋日報 {res.date}",
        "",
        f"產生時間：{now.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 今日新回饋：{res.new_feedback} 筆",
        f"- 今日涉及問題：{len(res.touched_issue_ids)} 個（共同問題 {len(common)}、單一中心問題 {len(single)}）",
        f"- 目前未完成問題總數：{len(backlog)}",
        "",
        "## 一、共同問題（2 家以上中心反映）",
        "",
        *([_issue_md(db, i, res.date) + "\n" for i in common] or ["（無）\n"]),
        "## 二、單一中心問題",
        "",
        *([_issue_md(db, i, res.date) + "\n" for i in single] or ["（無）\n"]),
        "## 三、待處理清單（全部未完成）",
        "",
        "| # | 標題 | 類型 | 優先級 | 狀態 | 中心數 |",
        "|---|---|---|---|---|---|",
        *[f"| {i['id']} | {i['title']} | {KIND_ZH.get(i['kind'], i['kind'])} | {i['priority'] or '-'} | "
          f"{STATUS_ZH.get(i['status'], i['status'])} | {len(db.issue_groups(i['id']))} |" for i in backlog],
        "",
        "## 四、改完後怎麼標記",
        "",
        "- 照平常一樣 commit（`feat:`／`fix:` + 中文說明）並推到 master，隔天早上會自動配對到問題；",
        "  commit 訊息裡寫 `#編號`（例如 `fix: … (#12)`）配對最準。",
        "- 也可以 LINE 私訊機器人：`/resolve 編號 怎麼改的 | 在哪裡`",
        "",
    ]
    path = settings.reports_dir / f"{res.date}-digest.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _admin_digest_summary(db: DB, res: DigestResult) -> str:
    if not res.touched_issue_ids:
        return f"📋 {res.date} 回饋日報：今天沒有新的系統回饋。"
    lines = [f"📋 {res.date} 回饋日報", f"新回饋 {res.new_feedback} 筆，涉及 {len(res.touched_issue_ids)} 個問題", ""]
    for label, ids in (("🔁 共同問題", [i for i in res.touched_issue_ids if _is_common(db, i)]),
                       ("👤 單一問題", [i for i in res.touched_issue_ids if not _is_common(db, i)])):
        if not ids:
            continue
        lines.append(label)
        for i in ids:
            iss = db.issue(i)
            cos = "、".join(db.group_label(g) for g in db.issue_groups(i))
            lines.append(f"#{i} [{iss['priority'] or '-'}] {iss['title']}（{cos}）")
        lines.append("")
    lines.append("看細節：/issue 編號　改完：/resolve 編號 說明 | 位置")
    return "\n".join(lines)


# ====================================================================== resolve


def resolve_issue(db: DB, issue_id: int, note: str, location: str, now: datetime,
                  status: str = "resolved", commits: list[str] | None = None, auto: bool = False) -> bool:
    if not db.issue(issue_id):
        return False
    db.update_issue(issue_id, now, status=status, resolution_note=note, resolution_location=location,
                    resolved_at=now.isoformat(), fix_commits=json.dumps(commits or []), auto_resolved=int(auto))
    return True


def reopen_issue(db: DB, issue_id: int, now: datetime) -> bool:
    i = db.issue(issue_id)
    if not i:
        return False
    db.update_issue(issue_id, now, status="analyzed" if i["analysis"] else "open", resolution_note=None,
                    resolution_location=None, resolved_at=None, fix_commits=None, auto_resolved=0)
    return True


# ====================================================================== commits


@dataclass
class SyncResult:
    commits: int = 0
    resolved: list[int] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    admin_summary: str = ""


def _safe_update(repo: CodeRepo) -> None:
    try:
        repo.update()
    except Exception:  # noqa: BLE001
        log.exception("更新程式庫失敗，使用現有版本")


_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def sync_commits(db: DB, llm: LLM, settings: Settings, repo: CodeRepo, now: datetime | None = None) -> SyncResult:
    """讀取上次之後的新 commit，配對到未完成的問題。高信心自動標記完成；中信心列為建議。"""
    now = now or now_in(settings)
    res = SyncResult()
    if repo is None or not repo.exists():
        res.admin_summary = "（未設定 REPO_PATH，略過 commit 配對）"
        return res
    with RUN_LOCK:
        if settings.repo_auto_update:
            _safe_update(repo)
        last = db.get_kv("last_commit")
        commits = repo.commits_since(last)
        res.commits = len(commits)
        open_issues = db.open_issues()
        if commits and open_issues:
            issues_payload = [
                {"id": i["id"], "title": i["title"], "kind": i["kind"], "summary": i["summary"], "module": i["module"],
                 "user_path": (json.loads(i["analysis"]).get("user_path") if i["analysis"] else ""),
                 "feedback": [f["detail"] for f in db.issue_feedback(i["id"])][:5]}
                for i in open_issues
            ]
            matching = llm.match_commits(issues_payload, [c.as_dict() for c in commits],
                                         settings.system_context(repo), repo=repo)
            valid = {i["id"] for i in open_issues}
            threshold = _CONF_RANK.get(settings.commit_match_min_confidence, 2)
            for m in matching.matches:
                if m.issue_id not in valid:
                    continue
                if _CONF_RANK[m.confidence] >= threshold:
                    resolve_issue(db, m.issue_id, m.how_fixed, m.where, now, commits=m.commit_hashes, auto=True)
                    res.resolved.append(m.issue_id)
                elif m.confidence == "medium":
                    res.suggestions.append(
                        f"#{m.issue_id} 可能已由 {', '.join(m.commit_hashes)} 處理：{m.reason}\n"
                        f"確認的話回：/resolve {m.issue_id} {m.how_fixed} | {m.where}")
        if commits:
            db.set_kv("last_commit", commits[-1].hash)
        elif last is None:
            db.set_kv("last_commit", repo.head())
        res.admin_summary = _admin_sync_summary(db, res)
        return res


def _admin_sync_summary(db: DB, res: SyncResult) -> str:
    lines = [f"🔧 新 commit {res.commits} 個"]
    if res.resolved:
        lines.append("自動標記完成：")
        for i in res.resolved:
            iss = db.issue(i)
            lines.append(f"✅ #{i} {iss['title']}\n　改了：{iss['resolution_note']}\n　位置：{iss['resolution_location']}")
        lines.append("配錯的話回 /reopen 編號（公告送出前都可以改）")
    if res.suggestions:
        lines += ["", "🤔 可能相關、需要你確認："] + res.suggestions
    if not res.resolved and not res.suggestions:
        lines.append("沒有配對到已回報的問題。")
    return "\n".join(lines)


# ====================================================================== announce


@dataclass
class AnnounceResult:
    date: str
    draft_ids: list[int] = field(default_factory=list)
    evaluation_path: Path | None = None
    admin_summary: str = ""


def prepare_announcements(db: DB, llm: LLM, settings: Settings, now: datetime | None = None) -> AnnounceResult:
    now = now or now_in(settings)
    day = now.strftime("%Y-%m-%d")
    res = AnnounceResult(date=day)
    with RUN_LOCK:
        # ---- 成效評估：自上次評估後新完成的問題 ----
        since = db.get_kv("last_evaluation_at") or "0000"
        newly = db.q("SELECT * FROM issues WHERE status IN ('resolved','wontfix') AND resolved_at > ? ORDER BY id",
                     (since,))
        if newly:
            payload = [
                {"issue_id": i["id"], "title": i["title"], "status": i["status"], "summary": i["summary"],
                 "companies": [db.group_label(g) for g in db.issue_groups(i["id"])],
                 "feedback": _feedback_payload(db, i["id"]),
                 "resolution_note": i["resolution_note"], "resolution_location": i["resolution_location"],
                 "fix_commits": json.loads(i["fix_commits"] or "[]"), "auto_matched": bool(i["auto_resolved"])}
                for i in newly
            ]
            report = llm.evaluate(payload)
            res.evaluation_path = _write_evaluation(db, settings, day, newly, report, now)
            db.set_kv("last_evaluation_at", now.isoformat())

        # ---- 每個群組各自的修正公告 ----
        for g in db.q("SELECT * FROM groups WHERE active=1"):
            done = db.announced_issue_ids(g["id"])
            rows = db.q(
                """SELECT DISTINCT i.* FROM issues i JOIN feedback f ON f.issue_id=i.id
                   WHERE f.group_id=? AND i.status='resolved' ORDER BY i.id""", (g["id"],))
            todo = [i for i in rows if i["id"] not in done]
            if not todo:
                continue
            items = []
            for i in todo:
                mine = [f for f in db.issue_feedback(i["id"]) if f["group_id"] == g["id"]]
                items.append({
                    "problem": i["title"],
                    "what_users_said": [{"reporter": f["reporter"], "detail": f["detail"]} for f in mine],
                    "how_fixed": i["resolution_note"],
                    "where": i["resolution_location"],
                })
            text = llm.announcement(db.group_label(g["id"]), items)
            res.draft_ids.append(db.add_announcement(g["id"], day, text, [i["id"] for i in todo], now))

        res.admin_summary = _admin_announce_summary(db, res, settings.auto_push)
        return res


def _write_evaluation(db: DB, settings: Settings, day: str, issues: list, report, now: datetime) -> Path:
    ev = {e.issue_id: e for e in report.evaluations}
    mark = {"yes": "✅ 已回應", "partial": "🟡 部分回應", "no": "❌ 未回應", "unclear": "❔ 無法判斷"}
    lines = [f"# 修正成效評估 {day}", "", f"產生時間：{now.strftime('%Y-%m-%d %H:%M')}", "",
             "## 總評", "", report.overview, "",
             "## 各問題", "",
             "| # | 問題 | 狀態 | 中心 | 怎麼改 | 改在哪 | 評估 |", "|---|---|---|---|---|---|---|"]
    for i in issues:
        e = ev.get(i["id"])
        cos = "、".join(db.group_label(g) for g in db.issue_groups(i["id"]))
        lines.append(f"| {i['id']} | {i['title']} | {STATUS_ZH.get(i['status'])} | {cos} | "
                     f"{i['resolution_note'] or ''} | {i['resolution_location'] or ''} | "
                     f"{mark.get(e.addresses_feedback, '') + '：' + e.comment if e else ''} |")
    remaining = db.open_issues()
    lines += ["", "## 後續追蹤", "", *([f"- {s}" for s in report.follow_ups] or ["（無）"]), "",
              f"## 尚未完成（{len(remaining)}）", "",
              *[f"- #{i['id']} [{i['priority'] or '-'}] {i['title']}（{STATUS_ZH.get(i['status'])}）" for i in remaining],
              ""]
    path = settings.reports_dir / f"{day}-evaluation.md"
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _admin_announce_summary(db: DB, res: AnnounceResult, auto_push: bool) -> str:
    if not res.draft_ids:
        return f"📣 {res.date}：沒有需要公告的修正。"
    parts = [f"📣 {res.date} 修正公告草稿 {len(res.draft_ids)} 則", ""]
    for d in db.q(f"SELECT * FROM announcements WHERE id IN ({','.join('?' * len(res.draft_ids))})",
                  tuple(res.draft_ids)):
        parts += [f"【草稿 {d['id']}】→ {db.group_label(d['group_id'])}", d["text"], "—" * 10]
    parts.append("將自動推播。" if auto_push else "確認無誤請回覆 /approve（全部）或 /approve 草稿編號；不要發送：/skip 草稿編號")
    return "\n".join(parts)


def send_announcements(db: DB, line, now: datetime, ids: list[int] | None = None) -> list[tuple[int, str]]:
    """推播草稿。回傳 [(草稿編號, 結果)]。"""
    results = []
    for d in db.drafts():
        if ids and d["id"] not in ids:
            continue
        try:
            line.push(d["group_id"], d["text"])
            db.set_announcement_status(d["id"], "sent", now)
            results.append((d["id"], "已發送"))
        except Exception as e:  # noqa: BLE001
            log.exception("push failed")
            db.set_announcement_status(d["id"], "failed", now, error=str(e))
            results.append((d["id"], f"失敗：{e}"))
    return results
