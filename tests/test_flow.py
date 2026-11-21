"""端到端測試：用假的 LINE 與假的 Claude 跑完一整天的循環。"""
import base64
import hashlib
import hmac
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from feedbackbot import pipeline
from feedbackbot.app import Bot, create_app
from feedbackbot.config import Settings
from feedbackbot.db import DB
from feedbackbot.line_api import split_text, verify_signature
from feedbackbot.llm import (Analysis, Assignment, Clustering, Evaluation, EvaluationReport, Extraction,
                             ExtractedFeedback, NewIssue)

TZ = ZoneInfo("Asia/Taipei")
SECRET = "test-secret"
ADMIN = "Uadmin"
GA, GB = "Cgroup_aaaaaaaa", "Cgroup_bbbbbbbb"


class FakeLine:
    def __init__(self):
        self.pushes, self.replies = [], []

    def reply(self, token, text): self.replies.append(text)
    def push(self, to, text): self.pushes.append((to, text))
    def group_name(self, gid): return {GA: "甲公司-系統群", GB: "乙公司-系統群"}[gid]
    def member_name(self, gid, uid): return {"U1": "小王", "U2": "阿美", "U3": "老李"}.get(uid)
    def download_content(self, mid, dest): return None


class FakeLLM:
    RULES = [("匯出", "bug", "匯出缺出貨日期"), ("庫存", "feature", "低庫存提醒")]

    def extract(self, company, transcript, images):
        items = []
        for line in transcript.splitlines():
            m = re.match(r"#(\d+) \[[^\]]+\] (\S+?)(?:（[^）]*）)?: (.*)", line)
            for kw, kind, title in self.RULES:
                if m and kw in m.group(3):
                    items.append(ExtractedFeedback(message_ids=[int(m.group(1))], reporter=m.group(2), kind=kind,
                                                   title=title, detail=m.group(3), severity="medium"))
        return Extraction(items=items)

    def cluster(self, open_issues, new_feedback):
        assignments, new, keys = [], [], {}
        for f in new_feedback:
            existing = next((i["id"] for i in open_issues if i["title"] == f["title"]), 0)
            if existing:
                assignments.append(Assignment(feedback_id=f["feedback_id"], issue_id=existing, new_issue_key=""))
                continue
            if f["title"] not in keys:
                keys[f["title"]] = f"N{len(keys) + 1}"
                new.append(NewIssue(key=keys[f["title"]], title=f["title"], kind=f["kind"],
                                    summary=f["detail"], module="訂單管理"))
            assignments.append(Assignment(feedback_id=f["feedback_id"], issue_id=0, new_issue_key=keys[f["title"]]))
        return Clustering(assignments=assignments, new_issues=new)

    def analyze(self, issue, feedback, system_context):
        return Analysis(understanding="u", root_cause="匯出欄位未包含", proposed_changes=["加欄位"],
                        affected_area="訂單匯出", effort="S", priority="P1", risks=[], questions_to_confirm=[],
                        acceptance_checks=["匯出後有出貨日期"])

    def evaluate(self, resolved):
        return EvaluationReport(overview="整體良好", evaluations=[
            Evaluation(issue_id=r["issue_id"], addresses_feedback="yes", comment="ok") for r in resolved],
            follow_ups=[])

    def announcement(self, company, items):
        return f"{company} 您好：" + "；".join(f"{i['problem']} 已修正（{i['where']}）" for i in items)


def sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


@pytest.fixture
def env(tmp_path):
    s = Settings()
    s.line_channel_secret = SECRET
    s.admin_user_ids = [ADMIN]
    s.data_dir = tmp_path / "data"
    s.reports_dir = tmp_path / "reports"
    s.system_context_file = tmp_path / "none.md"
    s.auto_push = False
    line = FakeLine()
    bot = Bot(settings=s, db=DB(":memory:"), line=line, llm=FakeLLM())
    client = TestClient(create_app(bot, start_scheduler=False))
    return bot, line, client


_n = [0]


def post(client, *events):
    body = json.dumps({"events": list(events)}).encode()
    r = client.post("/callback", content=body, headers={"x-line-signature": sign(body)})
    assert r.status_code == 200


def text_ev(src, uid, text, ts=datetime(2026, 11, 21, 10, 0, tzinfo=TZ)):
    _n[0] += 1
    source = {"type": "user", "userId": uid} if src is None else {"type": "group", "groupId": src, "userId": uid}
    return {"type": "message", "replyToken": "r", "timestamp": int(ts.timestamp() * 1000), "source": source,
            "message": {"type": "text", "id": f"m{_n[0]}", "text": text}}


def test_signature(env):
    _, _, client = env
    r = client.post("/callback", content=b"{}", headers={"x-line-signature": "bad"})
    assert r.status_code == 400
    assert verify_signature(SECRET, b"x", sign(b"x"))


def test_split_text():
    parts = split_text("a\n" * 4000, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).count("a") == 4000


def test_full_daily_cycle(env):
    bot, line, client = env
    ts = int(datetime(2026, 11, 21, 9, 0, tzinfo=TZ).timestamp() * 1000)
    post(client, {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GA}},
         {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GB}})
    post(client,
         text_ev(GA, "U1", "訂單匯出 Excel 沒有出貨日期，很不方便"),
         text_ev(GA, "U1", "大家中午吃什麼？"),
         text_ev(GA, "U3", "可以加庫存不足提醒嗎"),
         text_ev(GB, "U2", "匯出報表少了出貨日"),
         text_ev(GA, ADMIN, "/company 甲公司"))  # 管理者指令不會被收進回饋
    assert bot.db.group_label(GA) == "甲公司"
    assert bot.db.one("SELECT COUNT(*) c FROM messages")["c"] == 4

    # ---- 晚上：整理 ----
    res = pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))
    assert res.new_feedback == 3
    report = res.report_path.read_text(encoding="utf-8")
    common = report.split("## 一、共同問題")[1].split("## 二、")[0]
    single = report.split("## 二、單一公司問題")[1].split("## 三、")[0]
    assert "匯出缺出貨日期" in common and "甲公司" in common and "乙公司" in common
    assert "低庫存提醒" in single
    assert "大家中午吃什麼" not in report
    export_issue = bot.db.one("SELECT id FROM issues WHERE title='匯出缺出貨日期'")["id"]

    # 隔天又有人回報同一問題 → 歸入既有問題，不新增
    post(client, text_ev(GB, "U2", "匯出還是沒有出貨日期喔", ts=datetime(2026, 11, 22, 10, 0, tzinfo=TZ)))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 22, 23, 30, tzinfo=TZ))
    assert bot.db.one("SELECT COUNT(*) c FROM issues")["c"] == 2

    # ---- 開發者改完：私訊機器人標記 ----
    post(client, text_ev(None, ADMIN, f"/resolve {export_issue} 匯出檔新增出貨日期欄位 | 訂單管理 > 匯出 Excel"))
    assert "已標記完成" in line.replies[-1]

    # ---- 隔天早上：評估 + 公告草稿 ----
    ann = pipeline.prepare_announcements(bot.db, bot.llm, bot.s, datetime(2026, 11, 23, 9, 0, tzinfo=TZ))
    assert len(ann.draft_ids) == 2  # 甲、乙都反映過
    assert "訂單管理 > 匯出 Excel" in ann.evaluation_path.read_text(encoding="utf-8")
    assert not line.pushes or all(to == ADMIN for to, _ in line.pushes)  # 尚未推播到群組

    post(client, text_ev(None, ADMIN, "/approve"))
    group_pushes = [to for to, _ in line.pushes if to in (GA, GB)]
    assert sorted(group_pushes) == [GA, GB]

    # 已公告過不會重複
    ann2 = pipeline.prepare_announcements(bot.db, bot.llm, bot.s, datetime(2026, 11, 24, 9, 0, tzinfo=TZ))
    assert ann2.draft_ids == []


def test_non_admin_dm_is_silent(env):
    _, line, client = env
    post(client, text_ev(None, "U1", "/issues"))
    assert line.replies == []
    post(client, text_ev(None, "U1", "/whoami"))
    assert "U1" in line.replies[-1]
