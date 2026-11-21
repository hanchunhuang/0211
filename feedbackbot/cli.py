"""命令列工具：python -m feedbackbot.cli <指令>

  digest                         立即整理回饋並產生日報
  announce                       產生成效評估與公告草稿
  send [草稿編號…]                發送公告草稿（不填=全部）
  issues                         列出未完成問題
  issue 編號                     顯示問題細節
  resolve 編號 "怎麼改" "在哪裡"   標記完成
  groups                         列出群組
  set-company 群組代碼 公司名      設定公司名稱
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
    s = sub.add_parser("send"); s.add_argument("ids", nargs="*", type=int)
    sub.add_parser("issues")
    s = sub.add_parser("issue"); s.add_argument("id", type=int)
    s = sub.add_parser("resolve"); s.add_argument("id", type=int); s.add_argument("note"); s.add_argument("where", nargs="?", default="")
    sub.add_parser("groups")
    s = sub.add_parser("set-company"); s.add_argument("code"); s.add_argument("company")
    s = sub.add_parser("serve"); s.add_argument("--host", default="0.0.0.0"); s.add_argument("--port", type=int, default=8000)
    a = p.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("feedbackbot.app:app", host=a.host, port=a.port)
        return

    from .app import Bot
    bot = Bot()
    ctx = commands.Ctx(db=bot.db, line=bot.line, now=bot.now, run_digest=bot.job_digest,
                       run_announce=bot.job_announce)
    if a.cmd == "digest":
        res = pipeline.digest(bot.db, bot.llm, bot.s, bot.now())
        print(res.admin_summary, f"\n\n日報：{res.report_path}")
    elif a.cmd == "announce":
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
    elif a.cmd == "set-company":
        print(commands.handle(ctx, f"/company {a.code} {a.company}"))


if __name__ == "__main__":
    main(sys.argv[1:])
