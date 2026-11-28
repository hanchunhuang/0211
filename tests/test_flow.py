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

    def extract(self, company, transcript, images, systems_catalog=""):
        self.transcripts.append(transcript)
        self.catalogs.append(systems_catalog)
        ids = re.findall(r"^- (\w+)：", systems_catalog, re.M)
        guess = ids[0] if ids else "unknown"
        items = []
        for line in transcript.splitlines():
            m = re.match(r"#(\d+) \[[^\]]+\] (\S+?)(?:（[^）]*）)?: (.*)", line)
            for kw, kind, title in self.RULES:
                if m and kw in m.group(3):
                    sys_id = self.system_for.get(kw, guess)
                    items.append(ExtractedFeedback(message_ids=[int(m.group(1))], reporter=m.group(2), kind=kind,
                                                   title=title, detail=m.group(3), severity="medium", system=sys_id))
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
        self.catalogs = []
        self.system_for = {}          # 關鍵字 → 指定的系統代號（測試多系統用）
        self.analyzed = []            # (issue 標題, 系統, 有沒有程式庫)
        self.analyze_had_repo = None

    def analyze(self, issue, feedback, system_context, repo=None, knowledge_file=None):
        self.analyze_had_repo = repo is not None
        self.analyzed.append((issue["title"], issue.get("system"), repo is not None))
        return Analysis(understanding="u", root_cause="匯出欄位未包含", proposed_changes=["加欄位"],
                        affected_area="訂單匯出", effort="S", priority="P1", risks=[], questions_to_confirm=[],
                        acceptance_checks=["匯出後有出貨日期"], files_to_change=[], user_path="訂單管理 → 匯出")

    def match_commits(self, open_issues, commits, system_context, repo=None, knowledge_file=None):
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

    def summarize_system(self, system_name, changes_md, open_issues):
        return f"### {system_name} 現況\n- 修改回報 {changes_md.count('## #')} 份"

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
    s.systems_file = str(tmp_path / "no-systems.json")   # 預設單一系統；多系統另有測試
    s.repo_auto_update = False
    s.auto_analyze = True   # 原本的測試假設自動分析；手動分析另有測試
    s.code_analysis = True  # 原本的測試假設會讀程式碼；手動模式另有測試
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
    common = report.split("### 共同問題")[1].split("### 單一單位問題")[0]
    single = report.split("### 單一單位問題")[1].split("## 待處理清單")[0]
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


def test_non_admin_dm_gets_notice(env):
    _, line, client = env
    post(client, text_ev(None, "U1", "/issues"))
    assert "僅供管理者使用" in line.replies[-1] and "沒有未完成" not in line.replies[-1]
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


def test_multiple_systems(env, tmp_path):
    """回饋依系統分類：只在同系統內合併、用該系統的程式碼分析、改系統後重新分析、各系統分別配對 commit。"""
    from feedbackbot.systems import Systems
    bot, line, client = env
    repos = {}
    for sid in ("daycare", "pharma"):
        p = tmp_path / sid
        p.mkdir()
        _git(p, "init", "-q", "-b", "master")
        _git(p, "config", "user.email", "t@example.com")
        _git(p, "config", "user.name", "t")
        (p / "README.md").write_text(f"# {sid}\n", encoding="utf-8")
        _git(p, "add", "-A")
        _git(p, "commit", "-q", "-m", "feat: 初始")
        repos[sid] = p
    (tmp_path / "systems.json").write_text(json.dumps([
        {"id": "daycare", "name": "日照系統", "description": "長照", "path": str(repos["daycare"])},
        {"id": "pharma", "name": "藥師雜誌", "description": "雜誌", "path": str(repos["pharma"])},
        {"id": "hcare", "name": "Hcare", "repo": "someone/hcare"},       # 沒有權杖：不查程式碼
    ], ensure_ascii=False), encoding="utf-8")
    bot.s.systems_file = str(tmp_path / "systems.json")
    bot.systems = Systems.from_settings(bot.s)
    assert bot.systems.repo("hcare") is None

    ts = int(datetime(2026, 11, 21, 9, 0, tzinfo=TZ).timestamp() * 1000)
    post(client, {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GA}},
         {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GB}})
    bot.db.set_group_systems(GA, ["daycare"])          # 甲只用日照系統
    bot.llm.system_for = {"匯出": "daycare", "庫存": "pharma"}
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期"), text_ev(GB, "U2", "匯出少了出貨日"),
         text_ev(GB, "U3", "可以加庫存提醒嗎"))
    res = pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ), systems=bot.systems)
    assert "- daycare：日照系統" in bot.llm.catalogs[0] and "pharma" not in bot.llm.catalogs[0]  # 甲只給日照
    export, stock = issue_by_title(bot, "匯出缺出貨日期"), issue_by_title(bot, "低庫存提醒")
    assert export["system_id"] == "daycare" and stock["system_id"] == "pharma"
    assert len(bot.db.issue_groups(export["id"])) == 2                     # 同系統、跨單位 → 共同問題
    assert ("低庫存提醒", "藥師雜誌", True) in bot.llm.analyzed            # 用藥師雜誌的程式碼分析
    report = bot.db.report(res.report_name)["content"]
    assert "## 日照系統" in report and "## 藥師雜誌" in report

    # AI 判斷錯 → 改系統：清掉分析，下次整理用新系統重新分析
    post(client, text_ev(None, ADMIN, f"/system {stock['id']} hcare"))
    assert "Hcare" in line.replies[-1]
    assert bot.db.issue(stock["id"])["analysis"] is None
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 22, 23, 30, tzinfo=TZ), systems=bot.systems)
    assert bot.llm.analyzed[-1] == ("低庫存提醒", "Hcare", False)        # Hcare 沒權杖：不查程式碼
    assert bot.db.issue(stock["id"])["status"] == "analyzed"

    # commit 配對：各系統分別讀取，只配對該系統的問題
    pipeline.sync_commits(bot.db, bot.llm, bot.s, systems=bot.systems)
    (repos["daycare"] / "a.php").write_text("x")
    _git(repos["daycare"], "add", "-A")
    _git(repos["daycare"], "commit", "-qm", "fix: 訂單匯出加入出貨日期")
    sync = pipeline.sync_commits(bot.db, bot.llm, bot.s, systems=bot.systems)
    assert sync.commits == 1 and sync.resolved == [export["id"]]
    assert "Hcare" in sync.admin_summary  # 讀不到程式碼的系統會提示

    # 後台：依系統篩選、各系統統計、改系統
    bot.s.admin_web_password = "pw"
    origin = {"origin": "http://testserver"}
    client.post("/admin/login", data={"password": "pw"}, headers=origin)
    r = client.get("/admin/issues?status=all&system=hcare")
    assert "低庫存提醒" in r.text and "匯出缺出貨日期" not in r.text
    assert "各系統" in client.get("/admin").text
    client.post(f"/admin/issues/{stock['id']}/system", data={"system": "pharma"}, headers=origin)
    assert bot.db.issue(stock["id"])["system_id"] == "pharma"
    client.post(f"/admin/groups/{GB}/systems", data={"systems": ["pharma", "nope"]}, headers=origin)
    assert bot.db.group(GB)["systems"] == ["pharma"]


def test_direct_message_feedback(env):
    """私訊時一定有回應：非管理者會被告知、管理者打錯會提示，全形斜線也能用。"""
    bot, line, client = env
    post(client, text_ev(None, "Ustranger", "/sync"))
    assert "僅供管理者使用" in line.replies[-1]
    post(client, text_ev(None, ADMIN, "早安"))
    assert "/help" in line.replies[-1]
    post(client, text_ev(None, ADMIN, " ／issues "))
    assert "沒有未完成的問題" in line.replies[-1]


def test_long_command_runs_through_task_endpoint(env, monkeypatch):
    """在 Cloud Run 上，/sync 等長時間指令改呼叫自己的 /tasks/*，避免機器中途被關掉。"""
    import feedbackbot.app as appmod
    bot, line, client = env
    bot.s.task_token = "tok"
    bot.self_url = "https://feedbackbot-x.a.run.app"
    called = []

    def fake_post(url, headers=None, timeout=None, **kw):
        called.append(url)
        job = url.rsplit("/", 1)[1]
        return client.post(f"/tasks/{job}", headers=headers)

    monkeypatch.setattr(appmod.httpx, "post", fake_post)
    monkeypatch.setattr(appmod, "_spawn", lambda fn, *args: fn(*args))
    post(client, text_ev(None, ADMIN, "/sync"))
    assert "處理中" in line.replies[-1]
    assert called == ["https://feedbackbot-x.a.run.app/tasks/sync"]
    assert any("commit" in t for _, t in line.pushes)   # /tasks/sync 完成後通知管理者


def test_excluded_group_is_not_collected(env):
    bot, line, client = env
    ts = int(datetime(2026, 11, 21, 9, 0, tzinfo=TZ).timestamp() * 1000)
    post(client, {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GA}})
    post(client, text_ev(GA, "U1", "匯出報表沒有出貨日期"))           # 設定前已收集
    bot.s.admin_web_password = "pw"
    origin = {"origin": "http://testserver"}
    client.post("/admin/login", data={"password": "pw"}, headers=origin)
    client.post(f"/admin/groups/{GA}/exclude", data={"action": "exclude"}, headers=origin)
    post(client, text_ev(GA, "U1", "匯出又壞了"))                     # 設定後不收集
    assert bot.db.count_messages(group_id=GA) == 1
    res = pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))
    assert res.new_feedback == 0 and bot.db.count_messages(unprocessed=True) == 0
    r = client.get("/admin/units")
    assert "不收集的群組" in r.text
    client.post(f"/admin/groups/{GA}/exclude", data={"action": "include"}, headers=origin)
    post(client, text_ev(GA, "U1", "匯出還是沒有出貨日期"))
    assert bot.db.count_messages(group_id=GA) == 2


def test_fix_prompt(env):
    bot, line, client = env
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期"))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))
    iid = issue_by_title(bot, "匯出缺出貨日期")["id"]
    post(client, text_ev(None, ADMIN, f"/fix {iid}"))
    text = line.replies[-1]
    assert f"#{iid}" in text and "訂單匯出沒有出貨日期" in text and f"fix: <中文說明> (#{iid})" in text
    assert "不要自行 push" in text
    bot.s.admin_web_password = "pw"
    client.post("/admin/login", data={"password": "pw"}, headers={"origin": "http://testserver"})
    r = client.get(f"/admin/issues/{iid}")
    assert "複製問題說明" in r.text and "初步分析" in r.text


def test_summary_only_group(env):
    """「只歸納不分析」：收集並整理成問題，但不讀程式碼分析；有完整模式的群組也反映時才分析。"""
    bot, line, client = env
    ts = int(datetime(2026, 11, 21, 9, 0, tzinfo=TZ).timestamp() * 1000)
    post(client, {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GA}},
         {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GB}})
    bot.s.admin_web_password = "pw"
    origin = {"origin": "http://testserver"}
    client.post("/admin/login", data={"password": "pw"}, headers=origin)
    client.post(f"/admin/groups/{GA}/mode", data={"mode": "summary"}, headers=origin)
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期"))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))
    export = issue_by_title(bot, "匯出缺出貨日期")
    assert export["status"] == "collected" and not export.get("analysis") and bot.llm.analyzed == []
    assert "只歸納" in client.get("/admin/units").text
    # 完整模式的群組也反映同一問題 → 這時才分析
    post(client, text_ev(GB, "U2", "匯出少了出貨日"))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 22, 23, 30, tzinfo=TZ))
    assert bot.db.issue(export["id"])["status"] == "analyzed"
    # 切到不收集
    client.post(f"/admin/groups/{GA}/mode", data={"mode": "off"}, headers=origin)
    assert bot.db.is_excluded(GA)


def test_manual_analysis(env, monkeypatch):
    """預設不自動分析：每晚只歸納；管理者私訊 /analyze 或在後台按按鈕才讀程式碼分析。"""
    import feedbackbot.app as appmod
    bot, line, client = env
    bot.s.auto_analyze = False
    monkeypatch.setattr(appmod, "_spawn", lambda fn, *args: fn(*args))
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期"), text_ev(GA, "U3", "希望有庫存提醒"))
    res = pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))
    assert bot.llm.analyzed == []
    export, stock = issue_by_title(bot, "匯出缺出貨日期"), issue_by_title(bot, "低庫存提醒")
    assert export["status"] == "open"
    # LINE：/analyze 編號
    post(client, text_ev(None, ADMIN, f"/analyze {export['id']}"))
    assert "開始分析" in line.replies[-1]
    assert bot.db.issue(export["id"])["status"] == "analyzed" and len(bot.llm.analyzed) == 1
    assert any("分析完成" in t for _, t in line.pushes)
    assert bot.db.issue(stock["id"])["status"] == "open"            # 沒要求的不分析
    # 後台按鈕
    bot.s.admin_web_password = "pw"
    origin = {"origin": "http://testserver"}
    client.post("/admin/login", data={"password": "pw"}, headers=origin)
    assert "分析這個問題" in client.get(f"/admin/issues/{stock['id']}").text
    client.post(f"/admin/issues/{stock['id']}/analyze", headers=origin)
    assert bot.db.issue(stock["id"])["status"] == "analyzed"
    # 不存在的編號
    post(client, text_ev(None, ADMIN, "/analyze 999"))
    assert "找不到" in line.replies[-1]


def test_manual_mode_collaboration_and_unit_report(env):
    """手動模式：不讀程式碼；可編輯、合併、拆分問題、留備註；單位報表列出每天／每月的提問與解決。"""
    bot, line, client = env
    bot.s.code_analysis = False
    bot.s.auto_analyze = False
    ts = int(datetime(2026, 11, 21, 9, 0, tzinfo=TZ).timestamp() * 1000)
    post(client, {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GA}},
         {"type": "join", "timestamp": ts, "source": {"type": "group", "groupId": GB}})
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期", ts=datetime(2026, 11, 21, 10, 0, tzinfo=TZ)),
         text_ev(GA, "U3", "可以加庫存提醒嗎", ts=datetime(2026, 11, 21, 10, 5, tzinfo=TZ)),
         text_ev(GB, "U2", "匯出少了出貨日", ts=datetime(2026, 11, 21, 11, 0, tzinfo=TZ)))
    res = pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))
    assert bot.llm.analyzed == []
    assert "【甲公司-系統群】" in res.admin_summary and "【乙公司-系統群】" in res.admin_summary
    post(client, text_ev(None, ADMIN, "/sync"))
    assert "手動模式" in line.replies[-1]
    post(client, text_ev(None, ADMIN, "/analyze 1"))
    assert "手動模式" in line.replies[-1]

    bot.s.admin_web_password = "pw"
    origin = {"origin": "http://testserver"}
    client.post("/admin/login", data={"password": "pw"}, headers=origin)
    export, stock = issue_by_title(bot, "匯出缺出貨日期"), issue_by_title(bot, "低庫存提醒")
    page = client.get(f"/admin/issues/{export['id']}").text
    assert "分析這個問題" not in page and "編輯問題" in page and "協作備註" in page

    # 編輯：標題、負責人、優先級
    client.post(f"/admin/issues/{export['id']}/edit", headers=origin, data={
        "title": "匯出報表缺出貨日期", "summary": "兩家都反映", "kind": "bug", "system": export["system_id"],
        "priority": "P1", "module": "訂單", "assignee": "Becca"})
    e = bot.db.issue(export["id"])
    assert e["title"] == "匯出報表缺出貨日期" and e["assignee"] == "Becca" and e["priority"] == "P1"
    # 備註（記住署名）
    r = client.post(f"/admin/issues/{export['id']}/note", headers=origin, data={"author": "Eva", "text": "已和 Becca 討論"},
                    follow_redirects=False)
    assert "fbot_author" in r.headers["set-cookie"]
    assert json.loads(bot.db.issue(export["id"])["notes"])[0]["author"] == "Eva"
    assert 'value="Eva"' in client.get(f"/admin/issues/{export['id']}").text
    # 把一筆回饋拆成新問題，再合併回去
    fb_b = next(f for f in bot.db.issue_feedback(export["id"]) if f["group_id"] == GB)
    r = client.post(f"/admin/feedback/{fb_b['id']}/move", data={"target": "new"}, headers=origin, follow_redirects=False)
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert len(bot.db.issue_feedback(export["id"])) == 1 and len(bot.db.issue_feedback(new_id)) == 1
    client.post(f"/admin/issues/{new_id}/merge", data={"target": export["id"], "author": "Eva"}, headers=origin)
    assert bot.db.issue(new_id)["status"] == "merged" and len(bot.db.issue_feedback(export["id"])) == 2
    assert new_id not in [i["id"] for i in bot.db.open_issues()]

    # 解決後，單位報表
    post(client, text_ev(None, ADMIN, f"/resolve {export['id']} 匯出加上出貨日期 | 訂單 → 匯出"))
    bot.db.update_issue(export["id"], datetime(2026, 11, 22, 9, 0, tzinfo=TZ), resolved_at="2026-11-22T09:00:00+08:00")
    r = client.get("/admin/unit-report?period=month&date=2026-11")
    assert "甲公司-系統群" in r.text and "乙公司-系統群" in r.text and "匯出加上出貨日期" in r.text
    r = client.get(f"/admin/unit-report?period=day&date=2026-11-21&group={GA}")
    assert "訂單匯出沒有出貨日期" in r.text and "可以加庫存提醒嗎" in r.text and "匯出少了出貨日" not in r.text
    r = client.get("/admin/unit-report?period=day&date=2026-11-22")
    assert "匯出加上出貨日期" in r.text and "訂單匯出沒有出貨日期" not in r.text   # 22 日：只有解決，沒有新提問
    csv_text = client.get("/admin/unit-report.csv?period=month&date=2026-11").content.decode("utf-8-sig")
    assert "詢問" in csv_text and "已解決" in csv_text and "Becca" in csv_text
    assert client.get("/admin/unit-report?period=month&date=bad").status_code == 200


def test_issue_status_filter(env):
    bot, line, client = env
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期"), text_ev(GA, "U3", "可以加庫存提醒嗎"))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))   # 測試預設會自動分析
    stock = issue_by_title(bot, "低庫存提醒")
    bot.db.update_issue(stock["id"], datetime(2026, 11, 22, tzinfo=TZ), status="open")
    bot.s.admin_web_password = "pw"
    client.post("/admin/login", data={"password": "pw"}, headers={"origin": "http://testserver"})
    all_open = client.get("/admin/issues?status=open").text
    only_open = client.get("/admin/issues?status=status_open").text
    assert "匯出缺出貨日期" in all_open and "低庫存提醒" in all_open
    assert "低庫存提醒" in only_open and "匯出缺出貨日期" not in only_open


def test_fix_report_flow(env, monkeypatch):
    """處理完問題 → 貼上修改回報 md → 問題標記完成、記入系統修改紀錄 → 可下載紀錄、整理現況摘要。"""
    import feedbackbot.app as appmod
    from feedbackbot import fixreport
    bot, line, client = env
    monkeypatch.setattr(appmod, "_spawn", lambda fn, *args: fn(*args))
    bot.s.code_analysis = False
    post(client, text_ev(GA, "U1", "訂單匯出沒有出貨日期"), text_ev(GA, "U3", "可以加庫存提醒嗎"))
    pipeline.digest(bot.db, bot.llm, bot.s, datetime(2026, 11, 21, 23, 30, tzinfo=TZ))
    export, stock = issue_by_title(bot, "匯出缺出貨日期"), issue_by_title(bot, "低庫存提醒")
    sid = export["system_id"]
    assert fixreport.HEADER in pipeline.fix_prompt(bot.db, bot.systems, export["id"])   # 問題說明附上回報格式

    bot.s.admin_web_password = "pw"
    origin = {"origin": "http://testserver"}
    client.post("/admin/login", data={"password": "pw"}, headers=origin)
    page = client.get(f"/admin/fixes/new?issues={export['id']}").text
    assert f"## #{export['id']}" in page

    bad = "# 修改回報\n- 問題：#999\n"
    assert "找不到問題 #999" in client.post("/admin/fixes/check", data={"md": bad}, headers=origin).text
    md = f"""# 修改回報
- 系統：{sid}
- 問題：#{export['id']}
- 日期：2026-11-28
- 修改者：Eva
- commit：abc1234

## #{export['id']} 匯出缺出貨日期
- 怎麼改的：匯出 Excel 新增「出貨日期」欄位
- 在哪裡：訂單管理 → 匯出 Excel

## 技術說明
- 修改檔案：app/Exports/OrderExport.php
"""
    r = client.post("/admin/fixes/check", data={"md": md}, headers=origin)
    assert "確認套用" in r.text and "匯出 Excel 新增「出貨日期」欄位" in r.text
    r = client.post("/admin/fixes/apply", data={"md": md}, headers=origin, follow_redirects=False)
    assert r.status_code == 303 and f"/admin/systems/{sid}" in r.headers["location"]
    e = bot.db.issue(export["id"])
    assert e["status"] == "resolved" and e["resolution_location"] == "訂單管理 → 匯出 Excel"
    ch = bot.db.changes(sid)
    assert len(ch) == 1 and "OrderExport.php" in ch[0]["md"] and ch[0]["author"] == "Eva"

    # LINE 直接貼修改回報
    md2 = f"# 修改回報\n- 問題：#{stock['id']}\n\n## 怎麼改的\n新增低庫存提醒\n\n## 在哪裡\n- 庫存 → 提醒設定\n"
    post(client, text_ev(None, ADMIN, md2))
    assert "已套用修改回報" in line.replies[-1] and bot.db.issue(stock["id"])["status"] == "resolved"
    # 手動 /resolve 也會記入紀錄
    post(client, text_ev(GA, "U1", "排班頁很慢"))
    bot.db.create_issue("排班頁很慢", "bug", "慢", None, datetime(2026, 11, 28, tzinfo=TZ), system_id=sid)
    slow = issue_by_title(bot, "排班頁很慢")
    post(client, text_ev(None, ADMIN, f"/resolve {slow['id']} 加快排班頁 | 人員排班"))
    assert len(bot.db.changes(sid)) == 3

    r = client.get(f"/admin/systems/{sid}")
    assert "OrderExport.php" in r.text and "新增低庫存提醒" in r.text
    log_md = client.get(f"/admin/systems/{sid}/changelog.md").text
    assert "修改紀錄" in log_md and "匯出 Excel 新增「出貨日期」欄位" in log_md
    client.post(f"/admin/systems/{sid}/summary", headers=origin)
    assert "現況" in bot.db.get_kv(f"system_summary:{sid}")
    assert "系統現況摘要" in client.get(f"/admin/systems/{sid}/changelog.md").text
    assert "系統" in client.get("/admin/systems").text
