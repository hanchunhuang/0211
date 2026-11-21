"""管理者指令（在 LINE 私訊機器人，或在群組中由管理者輸入）。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from .db import DB
from .pipeline import KIND_ZH, STATUS_ZH, resolve_issue, send_announcements

HELP = """🛠 管理指令
/today　立即整理目前收集到的回饋（產生日報）
/issues　列出未完成問題
/issue 編號　看問題細節與修改建議
/start 編號　標記為修改中
/resolve 編號 怎麼改的 | 在哪裡　標記完成
/wontfix 編號 原因　標記不處理
/announce　立即產生修正公告草稿與成效評估
/preview　看待發送的公告草稿
/approve [草稿編號…]　發送公告（不填=全部）
/skip 草稿編號　不發送該草稿
/groups　列出群組
/company 公司名　（在群組內輸入）設定此群組的公司名稱
/company 群組代碼 公司名　（私訊）設定公司名稱
/whoami　顯示你的 userId"""


@dataclass
class Ctx:
    db: DB
    line: object
    now: Callable
    run_digest: Callable[[], str]          # 回傳給管理者的摘要
    run_announce: Callable[[], str]
    group_id: str | None = None            # 指令下在哪個群組（私訊為 None）


def handle(ctx: Ctx, text: str) -> str:
    text = text.strip()
    cmd, _, rest = text.partition(" ")
    cmd = cmd.lower()
    rest = rest.strip()
    db = ctx.db

    if cmd in ("/help", "/h", "/?"):
        return HELP

    if cmd == "/today":
        return ctx.run_digest()

    if cmd == "/announce":
        return ctx.run_announce()

    if cmd == "/issues":
        rows = db.open_issues()
        if not rows:
            return "目前沒有未完成的問題 🎉"
        lines = [f"未完成問題 {len(rows)} 個："]
        for i in rows:
            n = len(db.issue_groups(i["id"]))
            tag = "🔁" if n >= 2 else "👤"
            lines.append(f"{tag} #{i['id']} [{i['priority'] or '-'}] {i['title']}（{STATUS_ZH.get(i['status'])}，{n}家）")
        return "\n".join(lines)

    if cmd == "/issue":
        iid = _int(rest)
        i = db.issue(iid) if iid else None
        if not i:
            return "找不到這個問題編號"
        cos = "、".join(db.group_label(g) for g in db.issue_groups(iid))
        out = [f"#{i['id']} {i['title']}", f"{KIND_ZH.get(i['kind'], i['kind'])}｜{i['priority'] or '-'}｜"
               f"{STATUS_ZH.get(i['status'])}｜{cos}", "", i["summary"], "", "回饋："]
        for f in db.issue_feedback(iid):
            out.append(f"• [{db.group_label(f['group_id'])}] {f['reporter'] or ''}：{f['detail']}")
        if i["analysis"]:
            a = json.loads(i["analysis"])
            out += ["", f"原因：{a['root_cause']}", "建議修改："]
            out += [f"{n}. {s}" for n, s in enumerate(a["proposed_changes"], 1)]
            out.append(f"影響：{a['affected_area']}（工作量 {a['effort']}）")
        if i["resolution_note"]:
            out += ["", f"處理：{i['resolution_note']}｜{i['resolution_location'] or ''}"]
        return "\n".join(out)

    if cmd == "/start":
        iid = _int(rest)
        if not iid or not db.issue(iid):
            return "找不到這個問題編號"
        db.update_issue(iid, ctx.now(), status="in_progress")
        return f"#{iid} 已標記為修改中"

    if cmd in ("/resolve", "/done"):
        m = re.match(r"#?(\d+)\s+(.+)", rest, re.S)
        if not m:
            return "格式：/resolve 編號 怎麼改的 | 在哪裡"
        note, _, where = m.group(2).partition("|")
        if not resolve_issue(db, int(m.group(1)), note.strip(), where.strip(), ctx.now()):
            return "找不到這個問題編號"
        tip = "" if where.strip() else "\n（建議加上「| 在哪裡」，公告時使用者才知道去哪看）"
        return f"✅ #{m.group(1)} 已標記完成，會在下次公告時通知相關群組。{tip}"

    if cmd == "/wontfix":
        m = re.match(r"#?(\d+)\s*(.*)", rest, re.S)
        if not m or not resolve_issue(db, int(m.group(1)), m.group(2).strip(), "", ctx.now(), status="wontfix"):
            return "格式：/wontfix 編號 原因"
        return f"#{m.group(1)} 已標記為不處理（不會公告到群組）"

    if cmd == "/preview":
        drafts = db.drafts()
        if not drafts:
            return "沒有待發送的草稿"
        out = []
        for d in drafts:
            st = "（上次發送失敗）" if d["status"] == "failed" else ""
            out += [f"【草稿 {d['id']}】→ {db.group_label(d['group_id'])}{st}", d["text"], "—" * 10]
        return "\n".join(out)

    if cmd == "/approve":
        ids = [int(x) for x in re.findall(r"\d+", rest)] or None
        results = send_announcements(db, ctx.line, ctx.now(), ids)
        if not results:
            return "沒有可發送的草稿"
        return "\n".join(f"草稿 {i}：{r}" for i, r in results)

    if cmd == "/skip":
        ids = [int(x) for x in re.findall(r"\d+", rest)]
        for i in ids:
            db.set_announcement_status(i, "skipped", ctx.now())
        return f"已略過草稿 {', '.join(map(str, ids))}" if ids else "格式：/skip 草稿編號"

    if cmd == "/groups":
        rows = db.q("SELECT * FROM groups ORDER BY joined_at")
        if not rows:
            return "機器人還沒有加入任何群組"
        return "\n".join(f"{'🟢' if g['active'] else '⚪'} {g['company'] or '-'}（{g['name'] or ''}）代碼 {g['id'][-8:]}"
                         for g in rows)

    if cmd == "/company":
        if ctx.group_id and rest:
            db.set_group_company(ctx.group_id, rest)
            return f"此群組公司名稱設為：{rest}"
        code, _, name = rest.partition(" ")
        g = db.one("SELECT id FROM groups WHERE id LIKE ?", (f"%{code}",)) if code else None
        if not g or not name.strip():
            return "格式：/company 群組代碼 公司名（代碼可用 /groups 查）"
        db.set_group_company(g["id"], name.strip())
        return f"已設定 {code} 的公司名稱：{name.strip()}"

    return "看不懂這個指令，輸入 /help 查看說明"


def _int(s: str) -> int | None:
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else None
