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
import os
import subprocess
import uuid

from feedbackbot.llm import (Analysis, Assignment, Clustering, CommitMatch, CommitMatching, Evaluation,
                             EvaluationReport, Extraction, ExtractedFeedback, NewIssue)
from feedbackbot.repo import CodeRepo

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
        self.transcripts.append(transcript)
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

    def __init__(self):
        self.transcripts = []
        self.analyze_had_repo = None

    def analyze(self, issue, feedback, system_context, repo=None):
        self.analyze_had_repo = repo is not None
        return Analysis(understanding="u", root_cause="匯出欄位未包含", proposed_changes=["加欄位"],
                        affected_area="訂單匯出", effort="S", priority="P1", risks=[], questions_to_confirm=[],
                        acceptance_checks=["匯出後有出貨日期"], files_to_change=[], user_path="訂單管理 → 匯出")

    def match_commits(self, open_issues, commits, system_context, repo=None):
        matches = []
        for c in commits:
            for i in open_issues:
                if "出貨日期" in c["subject"] and i["title"] == "匯出缺出貨日期":
                    matches.append(CommitMatch(issue_id=i["id"], commit_hashes=[c["hash"]], how_fixed="匯出加上出貨日期",
                                               where="訂單管理 → 匯出 Excel", confidence="high", reason="r"))
                if "庫存" in c["subject"] and i["title"] == "低庫存提醒":
                    matches.append(CommitMatch(issue_id=i["id"], commit_hashes=[c["hash"]], how_fixed="x",
                                               where="y", confidence="medium", reason="只做了一部分"))
        return CommitMatching(matches=matches)

    def evaluate(self, resolved):
        return EvaluationReport(overview="整體良好", evaluations=[
            Evaluation(issue_id=r["issue_id"], addresses_feedback="yes", comment="ok") for r in resolved],
            follow_ups=[])

    def announcement(self, company, items):
        return f"{company} 您好：" + "；".join(f"{i['problem']} 已修正（{i['where']}）" for i in items)


def sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


def _make_db(backend: str):
    if backend == "sqlite":
        return DB(":memory:")
    if not os.getenv("FIRESTORE_EMULATOR_HOST"):
        pytest.skip("沒有 Firestore 模擬器（設定 FIRESTORE_EMULATOR_HOST 才會測）")
    from feedbackbot.store import FirestoreStore
    return DB(FirestoreStore("demo-test", prefix=f"t{uuid.uuid4().hex[:8]}_"))


@pytest.fixture(params=["sqlite", "firestore"])
def env(tmp_path, request):
    s = Settings()
    s.line_channel_secret = SECRET
    s.admin_user_ids = [ADMIN]
    s.data_dir = tmp_path / "data"
    s.system_context_file = tmp_path / "none.md"
    s.auto_push = False
    s.staff_user_ids = ["Ustaff"]
    s.repo_auto_update = False
    line = FakeLine()
    bot = Bot(settings=s, db=_make_db(request.param), line=line, llm=FakeLLM())
    client = TestClient(create_app(bot, start_scheduler=False))
    return bot, line, client


_n = [0]


def issue_by_title(bot, title):
    return next(i for i in bot.db.all_issues() if i["title"] == title)


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
         text_ev(GA, "Ustaff", "這個我們確認一下"),
         text_ev(GA, ADMIN, "/center 甲公司"))  # 管理者指令不會被收進回饋
    assert bot.db.group_label(GA) == "甲公司"
    assert bot.db.count_messages() == 5

    # ---- 晚上：整理 ----
    res = pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))
    assert res.new_feedback == 3
    assert any("（客服）" in t for t in bot.llm.transcripts)
    report = bot.db.report(res.report_name)["content"]
    common = report.split("## 一、共同問題")[1].split("## 二、")[0]
    single = report.split("## 二、單一中心問題")[1].split("## 三、")[0]
    assert "匯出缺出貨日期" in common and "甲公司" in common and "乙公司" in common
    assert "低庫存提醒" in single
    assert "大家中午吃什麼" not in report
    export_issue = issue_by_title(bot, "匯出缺出貨日期")["id"]

    # 隔天又有人回報同一問題 → 歸入既有問題，不新增
    post(client, text_ev(GB, "U2", "匯出還是沒有出貨日期喔", ts=datetime(2026, 11, 22, 10, 0, tzinfo=TZ)))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 22, 23, 30, tzinfo=TZ))
    assert len(bot.db.all_issues()) == 2

    # ---- 開發者改完：私訊機器人標記 ----
    post(client, text_ev(None, ADMIN, f"/resolve {export_issue} 匯出檔新增出貨日期欄位 | 訂單管理 > 匯出 Excel"))
    assert "已標記完成" in line.replies[-1]

    # ---- 隔天早上：評估 + 公告草稿 ----
    ann = pipeline.prepare_announcements(bot.db, bot.llm, bot.s, datetime(2026, 11, 23, 9, 0, tzinfo=TZ))
    assert len(ann.draft_ids) == 2  # 甲、乙都反映過
    assert "訂單管理 > 匯出 Excel" in bot.db.report(ann.evaluation_name)["content"]
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


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def code_repo(tmp_path):
    path = tmp_path / "system"
    path.mkdir()
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "a.php").write_text("<?php // 訂單匯出\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "feat: 初始")
    return CodeRepo(path)


def test_repo_tools(code_repo):
    assert "a.php:1" in code_repo.search("訂單匯出")
    assert "（檔案不存在或不在專案內）" == code_repo.read("../../etc/passwd")
    (code_repo.path / "b.php").write_text("x\n")
    _git(code_repo.path, "add", "-A")
    _git(code_repo.path, "commit", "-q", "-m", "fix: 第二個", "-m", "說明內文")
    cs = code_repo.commits_since(None)
    assert [c.subject for c in cs] == ["feat: 初始", "fix: 第二個"]
    assert cs[-1].files == ["b.php"] and cs[-1].body == "說明內文"
    assert [c.subject for c in code_repo.commits_since(cs[0].hash)] == ["fix: 第二個"]


def test_commit_sync_resolves_issues(env, code_repo):
    bot, line, client = env
    bot.repo = code_repo
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期"), text_ev(GA, "U3", "希望有庫存提醒"))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ), repo=code_repo)
    assert bot.llm.analyze_had_repo is True
    # 第一次同步：只記錄基準（初始 commit 不配對到問題）
    pipeline.sync_commits(bot.db, bot.llm, bot.s, code_repo, datetime(2026, 11, 22, 8, 0, tzinfo=TZ))
    (code_repo.path / "a.php").write_text("<?php // 訂單匯出 出貨日期\n", encoding="utf-8")
    _git(code_repo.path, "commit", "-qam", "fix: 訂單匯出加入出貨日期欄位")
    (code_repo.path / "c.php").write_text("x")
    _git(code_repo.path, "add", "-A")
    _git(code_repo.path, "commit", "-qm", "feat: 庫存頁面調整")
    res = pipeline.sync_commits(bot.db, bot.llm, bot.s, code_repo, datetime(2026, 11, 23, 8, 0, tzinfo=TZ))
    assert res.commits == 2
    export = issue_by_title(bot, "匯出缺出貨日期")
    stock = issue_by_title(bot, "低庫存提醒")
    assert export["status"] == "resolved" and export["auto_resolved"] == 1
    assert export["resolution_location"] == "訂單管理 → 匯出 Excel"
    assert stock["status"] != "resolved" and any(f"/resolve {stock['id']}" in t for t in res.suggestions)
    # 再跑一次不會重複處理
    assert pipeline.sync_commits(bot.db, bot.llm, bot.s, code_repo).commits == 0
    # 配錯可以 /reopen
    post(client, text_ev(None, ADMIN, f"/reopen {export['id']}"))
    assert bot.db.issue(export["id"])["status"] == "analyzed"


def test_admin_web(env):
    bot, line, client = env
    ts = int(datetime(2026, 11, 21, 9, 0, tzinfo=TZ).timestamp() * 1000)
    post(client, {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GA}},
         {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GB}})
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期 <script>alert(1)</script>"),
         text_ev(GB, "U2", "匯出少了出貨日"), text_ev(GA, "U3", "希望有庫存提醒"))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))

    # 沒設密碼 → 後台關閉；設了密碼 → 需要登入
    assert client.get("/admin/login").status_code == 503
    bot.s.admin_web_password = "pw"
    r = client.get("/admin/issues?status=all", follow_redirects=False)
    assert r.status_code == 303 and "/admin/login?next=" in r.headers["location"]
    assert client.post("/admin/issues/1/resolve", data={"note": "x"}).status_code == 401
    origin = {"origin": "http://testserver"}
    r = client.post("/admin/login", data={"password": "wrong"}, headers=origin, follow_redirects=False)
    assert "error=1" in r.headers["location"]
    assert client.post("/admin/login", data={"password": "pw"}, follow_redirects=False).status_code == 403  # 沒有來源
    r = client.post("/admin/login", data={"password": "pw", "next": "//evil.example"}, headers=origin,
                    follow_redirects=False)
    assert r.headers["location"] == "/admin" and "__session" in r.cookies

    r = client.get("/admin")
    assert r.status_code == 200 and "甲公司-系統群" in r.text and "每日新回饋" in r.text
    r = client.get(f"/admin?group={GA}&days=30")
    assert r.status_code == 200

    r = client.get(f"/admin/groups/{GA}")
    assert r.status_code == 200 and "匯出缺出貨日期" in r.text and "共同（2 家）" in r.text
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text  # 群組訊息會被跳脫

    r = client.get("/admin/issues?scope=common")
    assert "匯出缺出貨日期" in r.text and "低庫存提醒" not in r.text
    iid = issue_by_title(bot, "匯出缺出貨日期")["id"]
    r = client.get(f"/admin/issues/{iid}")
    assert r.status_code == 200 and "乙公司-系統群" in r.text and "訂單管理 → 匯出" in r.text

    r = client.get("/admin/messages?q=庫存")
    assert "希望有庫存提醒" in r.text and "訂單匯出" not in r.text

    # 跨站表單會被擋；同源才可以
    assert client.post(f"/admin/issues/{iid}/resolve", data={"note": "x"}).status_code == 403
    r = client.post(f"/admin/issues/{iid}/resolve", data={"note": "匯出加上出貨日期", "location": "訂單管理 → 匯出"},
                    headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and bot.db.issue(iid)["status"] == "resolved"

    # 公告：從網頁發送
    pipeline.prepare_announcements(bot.db, bot.llm, bot.s, datetime(2026, 11, 22, 9, 0, tzinfo=TZ))
    r = client.get("/admin/announcements")
    assert "發送到群組" in r.text
    aid = bot.db.drafts()[0]["id"]
    client.post(f"/admin/announcements/{aid}/approve", headers={"origin": "http://testserver"})
    assert any(to in (GA, GB) for to, _ in line.pushes)
    r = client.get("/admin/reports")
    assert "日報" in r.text
    name = r.text.split("?name=")[1].split('"')[0]
    assert client.get(f"/admin/reports?name={name}").status_code == 200
    assert client.get("/admin/reports?name=../../etc/passwd").status_code == 404
    assert client.get("/admin/images/1").status_code == 404


def test_task_endpoints(env):
    bot, line, client = env
    assert client.post("/tasks/digest").status_code == 403  # 沒設 TASK_TOKEN 一律拒絕
    bot.s.task_token = "tok"
    assert client.post("/tasks/digest", headers={"x-task-token": "bad"}).status_code == 403
    assert client.post("/tasks/nope", headers={"x-task-token": "tok"}).status_code == 404
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期"))
    r = client.post("/tasks/digest", headers={"x-task-token": "tok"})
    assert r.status_code == 200 and "回饋日報" in r.json()["summary"]
    assert r.headers["cache-control"] == "private, no-store"
    assert bot.db.report_names()


def test_admin_behind_firebase_hosting(env):
    """經過 Firebase Hosting 轉送時，Origin 是對外網域、Host 是 Cloud Run 網址。"""
    bot, line, client = env
    bot.s.admin_web_password = "pw"
    hosting = {"origin": "https://feedback.example.com", "x-forwarded-host": "feedback.example.com",
               "x-forwarded-proto": "https"}
    r = client.post("/admin/login", data={"password": "pw"}, headers=hosting, follow_redirects=False)
    assert r.status_code == 303 and "secure" in r.headers["set-cookie"].lower()
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    other = {"origin": "https://evil.example"}
    assert client.post("/admin/login", data={"password": "pw"}, headers=other).status_code == 403


def test_line_api_failures_do_not_lose_messages(env):
    bot, line, client = env

    def boom(*a, **k):
        raise RuntimeError("LINE 暫時連不上")

    line.group_name = boom
    line.download_content = boom
    ts = int(datetime(2026, 11, 21, 9, 0, tzinfo=TZ).timestamp() * 1000)
    post(client, {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GA}})
    img = text_ev(GA, "U1", "")
    img["message"] = {"type": "image", "id": "img1"}
    post(client, img)
    assert bot.db.group(GA) is not None
    m = bot.db.messages_page(GA)[0][0]
    assert m["kind"] == "image" and m["image_ref"] is None


def test_units_page(env):
    bot, line, client = env
    ts = int(datetime(2026, 11, 21, 9, 0, tzinfo=TZ).timestamp() * 1000)
    post(client, {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GA}},
         {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GB}})
    line.bot_info = lambda: {"displayName": "福氣工程師 David", "basicId": "@abc"}
    bot.s.admin_web_password = "pw"
    client.post("/admin/login", data={"password": "pw"}, headers={"origin": "http://testserver"})
    r = client.get("/admin/units")
    assert "福氣工程師 David" in r.text and "2</div><div class=\"k\">未設定中心名稱" in r.text
    client.post(f"/admin/groups/{GA}/center", data={"center": "椰子園"}, headers={"origin": "http://testserver"})
    assert bot.db.group(GA)["center_set"] is True
    post(client, {"type": "leave", "timestamp": ts, "source": {"type": "group", "groupId": GB}})
    r = client.get("/admin/units")
    assert "已離開的群組" in r.text and "椰子園" in r.text
    assert "1</div><div class=\"k\">收集中的單位" in r.text
    assert any("開始收集" in t for _, t in line.pushes)
