"""每日循環：
1. digest()               每晚：萃取當天回饋 → 跨中心歸類（共同/單一）→ 查程式碼分析修改方式 → 產生日報
2. sync_commits()         讀取系統程式庫的新 commit，自動配對到已回報的問題並標記完成
   resolve_issue()        （或由管理者手動標記：怎麼改、改在哪裡）
3. prepare_announcements() 隔天早上：成效評估報告 + 為每個群組產生「已修正」公告草稿
4. send_announcements()   推播到群組（自動或經管理者 /approve）
"""
from __future__ import annotations

import json
import re
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import Settings
from .db import DB
from .llm import LLM
from .repo import CodeRepo
from . import fixreport
from .systems import UNKNOWN, Systems

log = logging.getLogger(__name__)
RUN_LOCK = threading.Lock()

KIND_ZH = {"bug": "錯誤", "feature": "新需求", "usability": "操作不便", "question": "使用疑問"}
STATUS_ZH = {"open": "待處理", "collected": "已歸納", "analyzed": "已分析", "merged": "已合併", "in_progress": "處理中", "resolved": "已完成", "wontfix": "不處理"}


def now_in(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings.timezone))


# ====================================================================== digest


@dataclass
class DigestResult:
    date: str
    new_feedback: int = 0
    touched_issue_ids: list[int] = field(default_factory=list)
    report_name: str | None = None
    admin_summary: str = ""


def _transcript(db: DB, rows: list, staff_ids: list[str] | None = None,
                files=None) -> tuple[str, list[tuple[int, bytes, str]]]:
    by_line_id = {r["line_message_id"]: r["id"] for r in rows if r["line_message_id"]}
    lines: list[str] = []
    images: list[tuple[int, bytes, str]] = []
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
            got = files.read(r["image_ref"]) if (files is not None and r.get("image_ref")) else None
            if got:
                images.append((r["id"], got[0], got[1]))
        else:
            body = f"[{r['kind']}]"
        lines.append(f"#{r['id']} [{t}] {name}{quote}: {body}")
    return "\n".join(lines), images


def _resolve_systems(settings: Settings, repo: CodeRepo | None, systems: Systems | None) -> Systems:
    if systems is not None:
        return systems
    if repo is not None:
        return Systems.single(settings, repo)
    return Systems.from_settings(settings)


def _group_candidates(db: DB, systems: Systems, group_id: str) -> list[str]:
    g = db.group(group_id) or {}
    chosen = [x for x in (g.get("systems") or []) if systems.valid(x)]
    return chosen or systems.ids()


def digest(db: DB, llm: LLM, settings: Settings, now: datetime | None = None,
           repo: CodeRepo | None = None, files=None, systems: Systems | None = None) -> DigestResult:
    now = now or now_in(settings)
    day = now.strftime("%Y-%m-%d")
    res = DigestResult(date=day)
    systems = _resolve_systems(settings, repo, systems)
    with RUN_LOCK:
        # ---- 1. 每個群組各自萃取回饋，並判斷屬於哪個系統 ----
        for group_id, rows in db.unprocessed_messages().items():
            if db.is_excluded(group_id):
                db.mark_processed([r["id"] for r in rows])  # 設成不收集之前留下的訊息：不分析
                continue
            company = db.group_label(group_id)
            transcript, images = _transcript(db, rows, settings.staff_user_ids, files)
            valid_ids = {r["id"] for r in rows}
            cands = _group_candidates(db, systems, group_id)
            if any(r["kind"] in ("text", "image") for r in rows):
                # 手動模式不下載程式碼，只用 systems.json 的說明判斷系統
                catalog = systems.catalog(only=cands, with_readme=settings.code_analysis and len(cands) > 1)
                msg_time = {r["id"]: r["created_at"] for r in rows}
                extraction = llm.extract(company, transcript, images[: settings.max_images_per_group], catalog)
                for it in extraction.items:
                    ids = [i for i in it.message_ids if i in valid_ids]
                    sid = it.system if systems.valid(it.system) else (cands[0] if len(cands) == 1 else UNKNOWN)
                    asked = min((msg_time[i] for i in ids), default=None)  # 實際提問時間（報表用）
                    db.add_feedback(group_id=group_id, message_ids=ids, reporter=it.reporter, kind=it.kind,
                                    title=it.title, detail=it.detail, severity=it.severity,
                                    digest_date=day, now=now, system_id=sid, asked_at=asked)
                    res.new_feedback += 1
            db.mark_processed([r["id"] for r in rows])

        # ---- 2. 同一個系統內，跨單位歸類（找出共同問題）----
        touched: set[int] = set()
        by_system: dict[str, list[dict]] = {}
        for f in db.unlinked_feedback():
            by_system.setdefault(f.get("system_id") or UNKNOWN, []).append(f)
        for sid, new_fb in by_system.items():
            touched |= _cluster_system(db, llm, sid, new_fb, now)

        # ---- 3. 修改分析 ----
        # 預設（AUTO_ANALYZE=false）：不自動讀程式碼，等管理者在後台按「分析」或私訊 /analyze 編號
        summary_groups = {g["id"] for g in db.groups() if g.get("summary_only")}
        if settings.auto_analyze:
            pending = {i["id"] for i in db.open_issues() if i["status"] == "open" and not i.get("analysis")}
            targets = sorted(touched | pending)
        else:
            targets = sorted(touched)
        updated: set[str] = set()
        for issue_id in targets:
            issue = db.issue(issue_id)
            if issue["status"] in ("resolved", "wontfix", "in_progress"):
                continue
            # 只來自「只歸納不分析」群組的問題：不讀程式碼分析
            gids = set(db.issue_groups(issue_id))
            if gids and gids <= summary_groups:
                if issue["status"] == "open":
                    db.update_issue(issue_id, now, status="collected")
                continue
            if settings.auto_analyze:
                _analyze_issue(db, llm, settings, systems, issue_id, now, updated)
        res.touched_issue_ids = sorted(touched)

        # ---- 4. 日報 ----
        res.report_name = _write_digest_report(db, settings, res, now, systems)
        res.admin_summary = _admin_digest_summary(db, res, systems)
        return res


def _analyze_issue(db: DB, llm: LLM, settings: Settings, systems: Systems, issue_id: int, now: datetime,
                   updated: set[str]) -> None:
    """讀該系統的程式碼，分析這個問題怎麼改（最耗 AI 額度的一步）。"""
    issue = db.issue(issue_id)
    sid = issue.get("system_id") or UNKNOWN
    sys_repo = systems.repo(sid)
    if sys_repo is not None and sid not in updated and settings.repo_auto_update and sys_repo.exists():
        _safe_update(sys_repo)
        updated.add(sid)
    usable = sys_repo if (sys_repo is not None and sys_repo.exists()) else None
    analysis = llm.analyze(
        {"id": issue_id, "system": systems.name(sid), "title": issue["title"], "kind": issue["kind"],
         "summary": issue["summary"], "module": issue["module"]},
        _feedback_payload(db, issue_id), systems.context(sid), repo=usable,
        knowledge_file=systems.knowledge_file(sid))
    fields = {"analysis": analysis.model_dump(), "priority": analysis.priority, "analysis_requested": False}
    if issue["status"] in ("open", "collected", "analyzed"):
        fields["status"] = "analyzed"
    db.update_issue(issue_id, now, **fields)


def request_analysis(db: DB, issue_id: int, now: datetime) -> str:
    """管理者要求分析某個問題。回傳空字串＝已排入；否則回傳不能分析的原因。"""
    i = db.issue(issue_id)
    if not i:
        return "找不到這個問題編號"
    if i["status"] in ("resolved", "wontfix"):
        return "這個問題已經結案，不需要分析"
    db.update_issue(issue_id, now, analysis_requested=True)
    return ""


def analyze_requested(db: DB, llm: LLM, settings: Settings, now: datetime | None = None,
                      systems: Systems | None = None) -> str:
    """分析所有被要求分析的問題，回傳給管理者的摘要。"""
    now = now or now_in(settings)
    systems = _resolve_systems(settings, None, systems)
    done, failed = [], []
    with RUN_LOCK:
        updated: set[str] = set()
        for i in db.all_issues():
            if not i.get("analysis_requested") or i["status"] in ("resolved", "wontfix"):
                continue
            try:
                _analyze_issue(db, llm, settings, systems, i["id"], now, updated)
                done.append(i["id"])
            except Exception as e:  # noqa: BLE001
                log.exception("分析 #%s 失敗", i["id"])
                db.update_issue(i["id"], now, analysis_requested=False)
                failed.append(f"#{i['id']}：{str(e)[:120]}")
    lines = []
    for iid in done:
        iss = db.issue(iid)
        a = json.loads(iss["analysis"])
        files = "、".join(f["path"] for f in a.get("files_to_change", [])[:3])
        lines.append(f"🔍 #{iid} {iss['title']}（{systems.name(iss.get('system_id'))}）分析完成\n"
                     f"原因：{a['root_cause'][:120]}" + (f"\n檔案：{files}" if files else "")
                     + f"\n修改指示：/fix {iid}")
    if failed:
        lines.append("⚠️ 分析失敗：" + "；".join(failed))
    return "\n\n".join(lines) or "沒有等待分析的問題。"


def _cluster_system(db: DB, llm: LLM, sid: str, new_fb: list[dict], now: datetime) -> set[int]:
    """把同一個系統的新回饋，歸入既有問題或建立新問題。回傳有變動的問題編號。"""
    touched: set[int] = set()
    open_issues = [
        {"id": i["id"], "title": i["title"], "kind": i["kind"], "summary": i["summary"],
         "module": i["module"], "companies": [db.group_label(g) for g in db.issue_groups(i["id"])]}
        for i in db.open_issues() if (i.get("system_id") or UNKNOWN) == sid
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
                key_to_issue[a.new_issue_key] = db.create_issue(n.title, n.kind, n.summary, n.module, now, system_id=sid)
            issue_id = key_to_issue[a.new_issue_key]
        else:
            continue
        db.link_feedback(a.feedback_id, issue_id)
        assigned.add(a.feedback_id)
        touched.add(issue_id)
    # 模型漏掉的回饋：自成一個新問題，確保不遺失
    for fid, f in fb_by_id.items():
        if fid not in assigned:
            issue_id = db.create_issue(f["title"], f["kind"], f["detail"], None, now, system_id=sid)
            db.link_feedback(fid, issue_id)
            touched.add(issue_id)
    return touched


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
    out = [f"#### #{i['id']} {i['title']}",
           f"- 類型：{KIND_ZH.get(i['kind'], i['kind'])}　優先級：{i['priority'] or '-'}　狀態：{STATUS_ZH.get(i['status'], i['status'])}",
           f"- 模組：{i['module'] or '-'}",
           f"- 回報單位：{'、'.join(companies)}",
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


def _by_system(systems: Systems, issue_ids: list[int], db: DB) -> list[tuple[str, list[int]]]:
    """依系統分組（系統清單的順序，未分類放最後）。"""
    groups: dict[str, list[int]] = {}
    for iid in issue_ids:
        groups.setdefault(db.issue(iid).get("system_id") or UNKNOWN, []).append(iid)
    order = systems.ids() + [k for k in groups if k not in systems.ids()]
    return [(k, groups[k]) for k in order if k in groups]


def _write_digest_report(db: DB, settings: Settings, res: DigestResult, now: datetime, systems: Systems) -> str:
    backlog = db.open_issues()
    sections = _by_system(systems, res.touched_issue_ids, db)
    lines = [
        f"# 回饋日報 {res.date}",
        "",
        f"產生時間：{now.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"- 今日新回饋：{res.new_feedback} 筆",
        f"- 今日涉及問題：{len(res.touched_issue_ids)} 個",
        *[f"  - {systems.name(sid)}：{len(ids)} 個（共同 {sum(_is_common(db, i) for i in ids)}）" for sid, ids in sections],
        f"- 目前未完成問題總數：{len(backlog)}",
        "",
    ]
    if not sections:
        lines += ["今天沒有新的系統回饋。", ""]
    for sid, ids in sections:
        common = [i for i in ids if _is_common(db, i)]
        single = [i for i in ids if not _is_common(db, i)]
        lines += [
            f"## {systems.name(sid)}",
            "",
            "### 共同問題（2 個以上單位反映）",
            "",
            *([_issue_md(db, i, res.date) + "\n" for i in common] or ["（無）\n"]),
            "### 單一單位問題",
            "",
            *([_issue_md(db, i, res.date) + "\n" for i in single] or ["（無）\n"]),
        ]
    lines += [
        "## 待處理清單（全部未完成）",
        "",
        "| # | 系統 | 標題 | 類型 | 優先級 | 狀態 | 單位數 |",
        "|---|---|---|---|---|---|---|",
        *[f"| {i['id']} | {systems.name(i.get('system_id'))} | {i['title']} | {KIND_ZH.get(i['kind'], i['kind'])} | "
          f"{i['priority'] or '-'} | {STATUS_ZH.get(i['status'], i['status'])} | {len(db.issue_groups(i['id']))} |"
          for i in backlog],
        "",
        "## 改完後怎麼標記",
        "",
        "- 後台問題頁：填「怎麼改的」與「在哪裡」後按「標記完成」",
        "- 或 LINE 私訊機器人：`/resolve 編號 怎麼改的 | 在哪裡`",
        "- 問題內容、系統、負責人都可以在後台直接編輯；重複的問題可以合併。",
        "",
    ]
    name = f"{res.date}-digest.md"
    db.save_report(name, "\n".join(lines), now)
    return name


def _admin_digest_summary(db: DB, res: DigestResult, systems: Systems) -> str:
    """每晚私訊管理者：依單位列出今天提出的問題。"""
    if not res.touched_issue_ids:
        return f"📋 {res.date} 回饋日報：今天沒有新的系統回饋。"
    today_fb = [f for f in db.all_feedback() if f["digest_date"] == res.date and f.get("issue_id")]
    by_group: dict[str, set[int]] = {}
    for f in today_fb:
        by_group.setdefault(f["group_id"], set()).add(f["issue_id"])
    lines = [f"📋 {res.date} 回饋日報", f"新回饋 {res.new_feedback} 筆，{len(by_group)} 個單位，涉及 {len(res.touched_issue_ids)} 個問題", ""]
    for gid, iids in sorted(by_group.items(), key=lambda x: db.group_label(x[0])):
        lines.append(f"【{db.group_label(gid)}】")
        for i in sorted(iids):
            iss = db.issue(i)
            tag = "🔁" if _is_common(db, i) else "👤"
            lines.append(f"{tag} #{i} [{systems.name(iss.get('system_id'))}] {iss['title']}")
        lines.append("")
    lines.append("🔁 多個單位都提到　👤 只有這個單位\n後台可編輯、合併問題，並指定負責人。處理好後：/resolve 編號 怎麼改的 | 在哪裡")
    return "\n".join(lines)


# ====================================================================== resolve


def resolve_issue(db: DB, issue_id: int, note: str, location: str, now: datetime,
                  status: str = "resolved", commits: list[str] | None = None, auto: bool = False) -> bool:
    if not db.issue(issue_id):
        return False
    db.update_issue(issue_id, now, status=status, resolution_note=note, resolution_location=location,
                    resolved_at=now.isoformat(), fix_commits=json.dumps(commits or []), auto_resolved=int(auto))
    return True


def fix_prompt(db: DB, systems: Systems, issue_id: int) -> str:
    """整理成可以直接貼給 Claude Code（或工程師）的修改指示。"""
    i = db.issue(issue_id)
    if not i:
        return ""
    sid = i.get("system_id")
    d = systems.get(sid)
    kind = "feat" if i["kind"] == "feature" else "fix"
    lines = [f"請修正「{systems.name(sid)}」的問題 #{i['id']}：{i['title']}"]
    if d and d.repo:
        lines.append(f"（程式庫：{d.repo}）")
    lines += ["", "## 使用者回饋"]
    for f in db.issue_feedback(issue_id):
        lines.append(f"- [{db.group_label(f['group_id'])}] {f['reporter'] or ''}：{f['detail']}")
    lines += ["", "## 問題摘要", i["summary"]]
    if i.get("analysis"):
        a = json.loads(i["analysis"])
        lines += ["", "## 初步分析（由 AI 讀程式碼產生，請以實際程式碼為準）",
                  f"- 理解：{a['understanding']}", f"- 可能原因：{a['root_cause']}", "- 建議修改："]
        lines += [f"  {n}. {s}" for n, s in enumerate(a["proposed_changes"], 1)]
        if a.get("files_to_change"):
            lines.append("- 可能要改的檔案：")
            lines += [f"  - {f['path']}：{f['change']}" for f in a["files_to_change"]]
        if a.get("user_path"):
            lines.append(f"- 使用者操作位置：{a['user_path']}")
        if a.get("risks"):
            lines.append("- 注意風險：" + "；".join(a["risks"]))
        if a.get("questions_to_confirm"):
            lines.append("- 尚待使用者確認：" + "；".join(a["questions_to_confirm"]))
        if a.get("acceptance_checks"):
            lines += ["", "## 驗收方式"] + [f"- {x}" for x in a["acceptance_checks"]]
    lines += [
        "", "## 要求",
        "1. 先閱讀相關程式碼確認原因；如果上面的分析不正確，以實際程式碼為準並告訴我。",
        "2. 遵守本專案 CLAUDE.md 的規範。",
        "3. 修改後執行相關測試，並說明我要怎麼手動驗證。",
        f"4. commit 訊息用：{kind}: <中文說明> (#{i['id']})",
        "5. 改完先讓我確認，不要自行 push。",
        "6. 完成後，請用下面的格式給我一份「修改回報」Markdown（我會貼回回饋系統）：",
        "",
        "```markdown",
        fixreport.template([i], sid or UNKNOWN, systems.name(sid), datetime.now().strftime("%Y-%m-%d")),
        "```",
    ]
    return "\n".join(lines)


def system_summary(db: DB, llm: LLM, systems: Systems, system_id: str, now: datetime) -> str:
    """用 AI 整理「這個系統目前被調整成什麼樣子」（只在管理者按下時執行）。"""
    changes = db.changes(system_id)[:80]
    md = "\n\n".join(f"### {c['date']} {c['title']}\n{c['md']}" for c in reversed(changes))
    open_issues = [{"id": i["id"], "title": i["title"], "summary": i["summary"]}
                   for i in db.open_issues() if (i.get("system_id") or UNKNOWN) == system_id][:50]
    text = llm.summarize_system(systems.name(system_id), md or "（還沒有修改回報）", open_issues)
    db.set_kv(f"system_summary:{system_id}", text)
    db.set_kv(f"system_summary_at:{system_id}", now.isoformat())
    return text


def merge_issues(db: DB, source_id: int, target_id: int, now: datetime, author: str = "") -> str:
    """把 source 的回饋全部併到 target，source 標記為已合併。回傳空字串＝成功；否則為原因。"""
    src, dst = db.issue(source_id), db.issue(target_id)
    if not src or not dst:
        return "找不到問題編號"
    if source_id == target_id:
        return "不能合併到自己"
    if dst["status"] == "merged":
        return f"#{target_id} 已經併到 #{dst.get('merged_into')}，請改合併到那一個"
    for f in db.issue_feedback(source_id):
        db.move_feedback(f["id"], target_id)
    db.update_issue(source_id, now, status="merged", merged_into=int(target_id))
    db.add_issue_note(target_id, author or "系統", f"合併了 #{source_id}「{src['title']}」的回饋", now)
    return ""


@dataclass
class FixApply:
    ok: bool
    system_id: str = ""
    issues: list[dict] = field(default_factory=list)      # [{"id", "title", "how", "where", "status"}]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    change_id: int | None = None


def _match_system(systems: Systems, text: str) -> str:
    t = (text or "").strip()
    if not t:
        return ""
    first = re.split(r"[（(\s]", t, maxsplit=1)[0]
    if systems.valid(first):
        return first
    for sid, name in systems.options():
        if name in t or t in name:
            return sid
    return ""


def check_fix_report(db: DB, systems: Systems, md: str) -> FixApply:
    """解析並檢查修改回報（不寫入）。"""
    from . import fixreport
    rep = fixreport.parse(md)
    res = FixApply(ok=False, errors=list(rep.errors))
    sid = _match_system(systems, rep.system)
    if rep.system and not sid:
        res.errors.append(f"看不懂系統「{rep.system}」，請填系統代號（例如 {', '.join(systems.ids()[:3])}）")
    for iid in rep.issue_ids:
        i = db.issue(iid)
        if not i:
            res.errors.append(f"找不到問題 #{iid}")
            continue
        if i["status"] == "merged":
            res.errors.append(f"#{iid} 已經併入 #{i.get('merged_into')}，請改回報 #{i.get('merged_into')}")
            continue
        if i["status"] in ("resolved", "wontfix"):
            res.warnings.append(f"#{iid} 原本已經是「{STATUS_ZH.get(i['status'])}」，會用這份回報更新說明")
        if sid and i.get("system_id") not in (sid, None) and i.get("system_id") != UNKNOWN:
            res.warnings.append(f"#{iid} 原本歸在「{systems.name(i.get('system_id'))}」，會改成「{systems.name(sid)}」")
        if not rep.where_for(iid):
            res.warnings.append(f"#{iid} 沒有寫「在哪裡」，公告裡就不會說明位置")
        res.issues.append({"id": iid, "title": i["title"], "how": rep.how_for(iid), "where": rep.where_for(iid),
                           "status": STATUS_ZH.get(i["status"], i["status"])})
    if not sid and res.issues:
        sids = {db.issue(x["id"]).get("system_id") for x in res.issues}
        sid = sids.pop() if len(sids) == 1 else ""
        if not sid:
            res.errors.append("這些問題屬於不同系統，請在「- 系統：」寫明是哪個系統")
    res.system_id = sid
    res.ok = not res.errors
    res.parsed = rep  # type: ignore[attr-defined]
    return res


def apply_fix_report(db: DB, systems: Systems, md: str, now: datetime, author: str = "") -> FixApply:
    """套用修改回報：把問題標記完成，並記入該系統的修改紀錄。"""
    res = check_fix_report(db, systems, md)
    if not res.ok:
        return res
    rep = res.parsed  # type: ignore[attr-defined]
    for x in res.issues:
        if (db.issue(x["id"]).get("system_id") or UNKNOWN) != res.system_id:
            db.update_issue(x["id"], now, system_id=res.system_id)
        resolve_issue(db, x["id"], x["how"], x["where"], now, commits=rep.commits)
    title = "、".join(f"#{x['id']} {x['title']}" for x in res.issues)
    res.change_id = db.add_change({
        "system_id": res.system_id, "issue_ids": [x["id"] for x in res.issues], "title": title[:300],
        "date": rep.date or now.strftime("%Y-%m-%d"), "author": rep.author or author, "commits": rep.commits,
        "md": md.strip(),
    }, now)
    return res


def record_resolution(db: DB, issue_id: int, now: datetime, author: str = "") -> None:
    """手動標記完成（後台表單、/resolve）也記入系統修改紀錄。"""
    i = db.issue(issue_id)
    if not i or i["status"] != "resolved":
        return
    md = "\n".join([fixreport.HEADER, f"- 問題：#{i['id']}", f"- 日期：{now.strftime('%Y-%m-%d')}",
                     f"- 修改者：{author}", "", f"## #{i['id']} {i['title']}",
                     f"- 怎麼改的：{i.get('resolution_note') or ''}", f"- 在哪裡：{i.get('resolution_location') or ''}"])
    db.add_change({"system_id": i.get("system_id") or UNKNOWN, "issue_ids": [i["id"]], "title": f"#{i['id']} {i['title']}",
                   "date": now.strftime("%Y-%m-%d"), "author": author, "commits": [], "md": md}, now)


def system_changelog_md(db: DB, systems: Systems, system_id: str) -> str:
    """某個系統的完整修改紀錄（可下載，給之後的開發參考）。"""
    lines = [f"# {systems.name(system_id)}：修改紀錄", ""]
    summary = db.get_kv(f"system_summary:{system_id}")
    if summary:
        lines += ["## 系統現況摘要", "", summary, ""]
    for c in db.changes(system_id):
        body = re.sub(r"^#\s*修改回報\s*\n", "", c["md"]).replace("\n## ", "\n### ")
        lines += [f"## {c['date']}　{c['title']}", f"（回報者：{c.get('author') or '-'}）", "", body, ""]
    return "\n".join(lines)


def split_feedback(db: DB, feedback_id: int, now: datetime) -> int:
    """把一筆回饋拆出來，成為新的問題。回傳新問題編號。"""
    f = db.s.get("feedback", feedback_id)
    if not f:
        raise KeyError("回饋不存在")
    iid = db.create_issue(f["title"], f["kind"], f["detail"], None, now, system_id=f.get("system_id") or UNKNOWN)
    db.move_feedback(feedback_id, iid)
    return iid


def set_issue_system(db: DB, issue_id: int, system_id: str, now: datetime) -> bool:
    """改正問題所屬的系統；未完成的問題會清掉舊分析，下次整理時用新系統的程式碼重新分析。"""
    i = db.issue(issue_id)
    if not i:
        return False
    fields: dict = {"system_id": system_id}
    if i["status"] not in ("resolved", "wontfix"):
        fields.update(analysis=None, priority=None, status="open", analysis_requested=False)
    db.update_issue(issue_id, now, **fields)
    for f in db.issue_feedback(issue_id):
        db.s.update("feedback", f["id"], {"system_id": system_id})
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
    per_system: list[tuple[str, int]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    admin_summary: str = ""


def _safe_update(repo: CodeRepo) -> None:
    try:
        repo.update()
    except Exception:  # noqa: BLE001
        log.exception("更新程式庫失敗，使用現有版本")


_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


def sync_commits(db: DB, llm: LLM, settings: Settings, repo: CodeRepo | None = None,
                 now: datetime | None = None, systems: Systems | None = None) -> SyncResult:
    """讀取各系統上次之後的新 commit，配對到該系統未完成的問題。高信心自動標記完成；中信心列為建議。"""
    now = now or now_in(settings)
    res = SyncResult()
    systems = _resolve_systems(settings, repo, systems)
    with RUN_LOCK:
        for sid in systems.ids():
            log.info("讀取 commit：%s", sid)
            sys_repo = systems.repo(sid)
            if sys_repo is None or not sys_repo.exists():
                res.skipped.append(systems.name(sid))
                continue
            if settings.repo_auto_update:
                _safe_update(sys_repo)
            key = f"last_commit:{sid}"
            last = db.get_kv(key)
            if last is None and sid == systems.ids()[0]:
                last = db.get_kv("last_commit")  # 舊版（單一系統）留下的紀錄
            commits = sys_repo.commits_since(last)
            res.commits += len(commits)
            if commits:
                res.per_system.append((systems.name(sid), len(commits)))
            open_issues = [i for i in db.open_issues() if (i.get("system_id") or UNKNOWN) == sid]
            if commits and open_issues:
                issues_payload = [
                    {"id": i["id"], "title": i["title"], "kind": i["kind"], "summary": i["summary"],
                     "module": i["module"],
                     "user_path": (json.loads(i["analysis"]).get("user_path") if i["analysis"] else ""),
                     "feedback": [f["detail"] for f in db.issue_feedback(i["id"])][:5]}
                    for i in open_issues
                ]
                matching = llm.match_commits(issues_payload, [c.as_dict() for c in commits], systems.context(sid),
                                             repo=sys_repo, knowledge_file=systems.knowledge_file(sid))
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
                            f"#{m.issue_id}（{systems.name(sid)}）可能已由 {', '.join(m.commit_hashes)} 處理：{m.reason}\n"
                            f"確認的話回：/resolve {m.issue_id} {m.how_fixed} | {m.where}")
            if commits:
                db.set_kv(key, commits[-1].hash)
            elif last is None:
                db.set_kv(key, sys_repo.head())
        res.admin_summary = _admin_sync_summary(db, res)
        log.info("commit 配對完成：%d 個 commit，自動完成 %d 個問題", res.commits, len(res.resolved))
        return res


def _admin_sync_summary(db: DB, res: SyncResult) -> str:
    lines = [f"🔧 新 commit {res.commits} 個"
             + (f"（{'、'.join(f'{n} {c} 個' for n, c in res.per_system)}）" if res.per_system else "")]
    if res.skipped:
        lines.append(f"⚠️ 讀不到程式碼、略過：{'、'.join(res.skipped)}")
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
    evaluation_name: str | None = None
    admin_summary: str = ""


def prepare_announcements(db: DB, llm: LLM, settings: Settings, now: datetime | None = None,
                          systems: Systems | None = None) -> AnnounceResult:
    now = now or now_in(settings)
    day = now.strftime("%Y-%m-%d")
    res = AnnounceResult(date=day)
    systems = _resolve_systems(settings, None, systems)
    with RUN_LOCK:
        # ---- 成效評估：自上次評估後新完成的問題 ----
        since = db.get_kv("last_evaluation_at") or "0000"
        newly = db.issues_resolved_since(since)
        if newly:
            payload = [
                {"issue_id": i["id"], "system": systems.name(i.get("system_id")), "title": i["title"],
                 "status": i["status"], "summary": i["summary"],
                 "companies": [db.group_label(g) for g in db.issue_groups(i["id"])],
                 "feedback": _feedback_payload(db, i["id"]),
                 "resolution_note": i["resolution_note"], "resolution_location": i["resolution_location"],
                 "fix_commits": json.loads(i["fix_commits"] or "[]"), "auto_matched": bool(i["auto_resolved"])}
                for i in newly
            ]
            report = llm.evaluate(payload)
            res.evaluation_name = _write_evaluation(db, settings, day, newly, report, now)
            db.set_kv("last_evaluation_at", now.isoformat())

        # ---- 每個群組各自的修正公告 ----
        for g in db.groups(active_only=True):
            if g.get("excluded"):
                continue
            done = db.announced_issue_ids(g["id"])
            todo = [i for i in db.issues_for_group(g["id"]) if i["status"] == "resolved" and i["id"] not in done]
            if not todo:
                continue
            items = []
            for i in todo:
                mine = [f for f in db.issue_feedback(i["id"]) if f["group_id"] == g["id"]]
                items.append({
                    "system": systems.name(i.get("system_id")),
                    "problem": i["title"],
                    "what_users_said": [{"reporter": f["reporter"], "detail": f["detail"]} for f in mine],
                    "how_fixed": i["resolution_note"],
                    "where": i["resolution_location"],
                })
            text = llm.announcement(db.group_label(g["id"]), items)
            res.draft_ids.append(db.add_announcement(g["id"], day, text, [i["id"] for i in todo], now))

        res.admin_summary = _admin_announce_summary(db, res, settings.auto_push)
        return res


def _write_evaluation(db: DB, settings: Settings, day: str, issues: list, report, now: datetime) -> str:
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
    name = f"{day}-evaluation.md"
    db.save_report(name, "\n".join(lines), now)
    return name


def _admin_announce_summary(db: DB, res: AnnounceResult, auto_push: bool) -> str:
    if not res.draft_ids:
        return f"📣 {res.date}：沒有需要公告的修正。"
    parts = [f"📣 {res.date} 修正公告草稿 {len(res.draft_ids)} 則", ""]
    for d in (db.announcement(i) for i in res.draft_ids):
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
