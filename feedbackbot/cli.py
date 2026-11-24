"""命令列工具：python -m feedbackbot.cli <指令>

  digest                         立即整理回饋並產生日報
  sync                           讀取新 commit 並自動配對已修正的問題
  announce                       讀取 commit + 產生成效評估與公告草稿
  send [草稿編號…]                發送公告草稿（不填=全部）
  issues                         列出未完成問題
  issue 編號                     顯示問題細節
  resolve 編號 "怎麼改" "在哪裡"   標記完成
  groups                         列出群組
  set-center 群組代碼 中心名稱     設定中心名稱
  check                          檢查設定（LINE、Claude、程式庫、後台密碼）
  serve [--port 8000]            啟動 webhook 伺服器與排程
"""
from __future__ import annotations

import argparse
import sys

from . import commands, pipeline


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="feedbackbot")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("digest")
    sub.add_parser("announce")
    sub.add_parser("sync")
    s = sub.add_parser("send"); s.add_argument("ids", nargs="*", type=int)
    sub.add_parser("issues")
    s = sub.add_parser("issue"); s.add_argument("id", type=int)
    s = sub.add_parser("resolve"); s.add_argument("id", type=int); s.add_argument("note"); s.add_argument("where", nargs="?", default="")
    sub.add_parser("groups")
    s = sub.add_parser("set-center"); s.add_argument("code"); s.add_argument("center")
    sub.add_parser("check")
    s = sub.add_parser("serve"); s.add_argument("--host", default="0.0.0.0"); s.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("feedbackbot.app:app", host=a.host, port=a.port)
        return

    if a.cmd == "check":
        sys.exit(0 if run_check() else 1)

    from .app import Bot
    bot = Bot()
    ctx = commands.Ctx(db=bot.db, line=bot.line, now=bot.now, run_digest=bot.job_digest,
                       run_announce=bot.job_announce, run_sync=bot.job_sync)
    if a.cmd == "digest":
        res = pipeline.digest(bot.db, bot.llm, bot.s, bot.now(), repo=bot.repo)
        print(res.admin_summary, f"\n\n日報：{res.report_path}")
    elif a.cmd == "sync":
        print(pipeline.sync_commits(bot.db, bot.llm, bot.s, bot.repo, bot.now()).admin_summary)
    elif a.cmd == "announce":
        if bot.repo is not None:
            print(pipeline.sync_commits(bot.db, bot.llm, bot.s, bot.repo, bot.now()).admin_summary, "\n")
        res = pipeline.prepare_announcements(bot.db, bot.llm, bot.s, bot.now())
        print(res.admin_summary)
        if res.evaluation_path:
            print(f"\n成效評估：{res.evaluation_path}")
    elif a.cmd == "send":
        for i, r in pipeline.send_announcements(bot.db, bot.line, bot.now(), a.ids or None):
            print(f"草稿 {i}：{r}")
    elif a.cmd == "issues":
        print(commands.handle(ctx, "/issues"))
    elif a.cmd == "issue":
        print(commands.handle(ctx, f"/issue {a.id}"))
    elif a.cmd == "resolve":
        print(commands.handle(ctx, f"/resolve {a.id} {a.note} | {a.where}"))
    elif a.cmd == "groups":
        print(commands.handle(ctx, "/groups"))
    elif a.cmd == "set-center":
        print(commands.handle(ctx, f"/center {a.code} {a.center}"))


if __name__ == "__main__":
    main(sys.argv[1:])


def run_check() -> bool:
    """部署後檢查各項設定是否可用。"""
    import anthropic
    import httpx

    from .config import Settings
    from .repo import CodeRepo

    s = Settings()
    ok = True

    def line(passed: bool, name: str, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"{'✅' if passed else '❌'} {name}{'：' + detail if detail else ''}")

    def warn(name: str, detail: str) -> None:
        print(f"⚠️  {name}：{detail}")

    # LINE
    if not s.line_channel_secret or not s.line_access_token:
        line(False, "LINE", "LINE_CHANNEL_SECRET / LINE_CHANNEL_ACCESS_TOKEN 未設定")
    else:
        try:
            r = httpx.get("https://api.line.me/v2/bot/info",
                          headers={"Authorization": f"Bearer {s.line_access_token}"}, timeout=15)
            if r.status_code == 200:
                line(True, "LINE", f"機器人名稱「{r.json().get('displayName')}」")
            else:
                line(False, "LINE", f"Access token 無效（HTTP {r.status_code}）")
        except httpx.HTTPError as e:
            line(False, "LINE", f"連不到 LINE：{e}")
    if not s.admin_user_ids:
        warn("管理者", "ADMIN_USER_IDS 未設定（私訊機器人 /whoami 取得後填入）")

    # Claude
    try:
        m = anthropic.Anthropic(max_retries=1).models.retrieve(s.claude_model)
        line(True, "Claude API", f"模型 {m.id} 可用")
    except anthropic.AuthenticationError:
        line(False, "Claude API", "ANTHROPIC_API_KEY 無效")
    except anthropic.NotFoundError:
        line(False, "Claude API", f"找不到模型 {s.claude_model}")
    except Exception as e:  # noqa: BLE001
        line(False, "Claude API", str(e)[:200])

    # 程式庫
    if not s.repo_path:
        warn("程式庫", "REPO_PATH 未設定：分析不會查程式碼，也不會自動配對 commit")
    else:
        repo = CodeRepo(s.repo_path, s.repo_branch)
        if not repo.exists():
            line(False, "程式庫", f"{s.repo_path} 不是 git 專案")
        else:
            try:
                repo._git("fetch", "--quiet", "origin", s.repo_branch, timeout=120)
                line(True, "程式庫", f"可讀取，最新 commit {repo._git('rev-parse', '--short', 'origin/' + s.repo_branch).strip()}")
            except Exception as e:  # noqa: BLE001
                line(False, "程式庫", f"git fetch 失敗（Token 過期或無權限？）{str(e)[:150]}")
            if not (repo.path / s.repo_knowledge_file).exists():
                warn("知識庫", f"找不到 {s.repo_knowledge_file}，公告的「在哪裡」會較不準")

    # 資料夾
    try:
        s.ensure_dirs()
        test = s.data_dir / ".write-test"
        test.write_text("ok")
        test.unlink()
        line(True, "資料夾", f"資料庫 {s.db_path.resolve()}")
    except OSError as e:
        line(False, "資料夾", f"無法寫入 {s.data_dir}：{e}")

    # 後台與通知
    if not s.admin_web_password:
        warn("網頁後台", "ADMIN_WEB_PASSWORD 未設定，/admin 目前關閉")
    elif len(s.admin_web_password) < 12:
        warn("網頁後台", "密碼太短，建議 12 碼以上")
    else:
        line(True, "網頁後台", f"帳號 {s.admin_web_user}")
    if s.telegram_bot_token and s.telegram_chat_id:
        line(True, "Telegram 通知", "已設定")
    print("\n" + ("全部檢查通過 🎉" if ok else "有項目需要修正（❌）"))
    return ok
