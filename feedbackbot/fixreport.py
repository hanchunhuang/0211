"""「修改回報」Markdown：處理完問題後，把怎麼改的、在哪裡回報給 David。

格式（範例）：

    # 修改回報
    - 系統：daycare
    - 問題：#12, #15
    - 日期：2026-11-28
    - 修改者：Eva
    - commit：abc1234

    ## #12 匯出缺出貨日期
    - 怎麼改的：匯出 Excel 新增「出貨日期」欄位
    - 在哪裡：訂單管理 → 匯出 Excel

    ## #15 低庫存提醒
    - 怎麼改的：……
    - 在哪裡：……

    ## 技術說明
    - 修改檔案：app/Exports/OrderExport.php
    - ……

    ## 注意事項
    - ……

「怎麼改的」「在哪裡」會出現在給客戶的公告；其他段落只留在系統修改紀錄裡給內部參考。
也可以不分問題，直接寫一段「## 怎麼改的」「## 在哪裡」，套用到所有列出的問題。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HEADER = "# 修改回報"
_KEYS = {
    "系統": "system", "問題": "issues", "日期": "date", "修改者": "author", "負責人": "author",
    "commit": "commits", "Commit": "commits", "COMMIT": "commits",
}
_HOW = ("怎麼改的", "怎麼改", "修改內容", "改了什麼")
_WHERE = ("在哪裡", "位置", "操作位置")


@dataclass
class ParsedReport:
    system: str = ""
    issue_ids: list[int] = field(default_factory=list)
    date: str = ""
    author: str = ""
    commits: list[str] = field(default_factory=list)
    per_issue: dict[int, dict[str, str]] = field(default_factory=dict)   # {12: {"how": .., "where": ..}}
    how: str = ""                                                         # 不分問題的共用說明
    where: str = ""
    title: str = ""
    errors: list[str] = field(default_factory=list)

    def how_for(self, iid: int) -> str:
        return (self.per_issue.get(iid, {}).get("how") or self.how).strip()

    def where_for(self, iid: int) -> str:
        return (self.per_issue.get(iid, {}).get("where") or self.where).strip()


def is_report(text: str) -> bool:
    return (text or "").lstrip().startswith(HEADER)


def _key_value(line: str) -> tuple[str, str] | None:
    m = re.match(r"^\s*[-*]?\s*\**([^:：*]+?)\**\s*[:：]\s*(.*)$", line)
    return (m.group(1).strip(), m.group(2).strip()) if m else None


def parse(text: str) -> ParsedReport:
    """解析修改回報。不完整的地方記在 errors，不丟例外。"""
    rep = ParsedReport()
    if not is_report(text):
        rep.errors.append(f"第一行要是「{HEADER}」")
        return rep
    section: tuple[str, int | None] = ("meta", None)   # (種類, 問題編號)
    field_name: str | None = None                       # 多行內容接續到哪個欄位
    for raw in text.splitlines()[1:]:
        line = raw.rstrip()
        h = re.match(r"^##\s+(.*)$", line)
        if h:
            heading = h.group(1).strip()
            m = re.match(r"^#?(\d+)\b\s*(.*)$", heading)
            if m:
                section, field_name = ("issue", int(m.group(1))), None
                rep.per_issue.setdefault(int(m.group(1)), {})
            elif any(heading.startswith(k) for k in _HOW):
                section, field_name = ("how", None), "how"
            elif any(heading.startswith(k) for k in _WHERE):
                section, field_name = ("where", None), "where"
            else:
                section, field_name = ("other", None), None
            continue
        kind, iid = section
        if kind == "meta":
            kv = _key_value(line)
            if kv and kv[0] in _KEYS:
                k, v = _KEYS[kv[0]], kv[1]
                if k == "issues":
                    rep.issue_ids += [int(x) for x in re.findall(r"\d+", v) if int(x) not in rep.issue_ids]
                elif k == "commits":
                    rep.commits += [x for x in re.split(r"[,\s，、]+", v) if x]
                else:
                    setattr(rep, k, v)
            continue
        if kind == "issue":
            kv = _key_value(line)
            if kv and any(kv[0].startswith(k) for k in _HOW):
                field_name = "how"
                rep.per_issue[iid]["how"] = kv[1]
                continue
            if kv and any(kv[0].startswith(k) for k in _WHERE):
                field_name = "where"
                rep.per_issue[iid]["where"] = kv[1]
                continue
            if field_name and line.strip() and not _key_value(line):
                rep.per_issue[iid][field_name] = (rep.per_issue[iid].get(field_name, "") + "\n" + line.strip()).strip()
            elif kv:
                field_name = None   # 其他欄位（例如「技術說明」）只留在紀錄裡
            continue
        if kind in ("how", "where") and line.strip():
            text_line = re.sub(r"^\s*[-*]\s*", "", line).strip()
            if kind == "how":
                rep.how = (rep.how + "\n" + text_line).strip()
            else:
                rep.where = (rep.where + "\n" + text_line).strip()
    # 範本裡的提示文字「（……）」沒有改掉的，視為沒填
    placeholder = re.compile(r"^（[^）]*）$")
    rep.how = "" if placeholder.match(rep.how) else rep.how
    rep.where = "" if placeholder.match(rep.where) else rep.where
    for v in rep.per_issue.values():
        for k in ("how", "where"):
            if placeholder.match(v.get(k, "")):
                v[k] = ""
    for iid in rep.per_issue:
        if iid not in rep.issue_ids:
            rep.issue_ids.append(iid)
    if not rep.issue_ids:
        rep.errors.append("沒有找到問題編號（「- 問題：#12」或「## #12 標題」）")
    for iid in rep.issue_ids:
        if not rep.how_for(iid):
            rep.errors.append(f"#{iid} 沒有寫「怎麼改的」")
    return rep


def template(issues: list[dict], system_id: str, system_name: str, today: str) -> str:
    """產生空白的修改回報（給人填，或附在問題說明裡讓 Claude 照著產生）。"""
    ids = ", ".join(f"#{i['id']}" for i in issues) or "#編號"
    lines = [HEADER, f"- 系統：{system_id}（{system_name}）", f"- 問題：{ids}", f"- 日期：{today}",
             "- 修改者：", "- commit：", ""]
    for i in issues or [{"id": "編號", "title": "問題標題"}]:
        lines += [f"## #{i['id']} {i['title']}", "- 怎麼改的：（用客戶看得懂的話，會出現在公告）",
                  "- 在哪裡：（客戶在系統哪裡看得到，例如：訂單管理 → 匯出 Excel）", ""]
    lines += ["## 技術說明", "- 修改檔案：", "- 改動內容：", "", "## 注意事項", "- "]
    return "\n".join(lines)
