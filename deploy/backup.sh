#!/bin/sh
# 每天備份資料庫與截圖，保留 30 天。用 crontab 執行：
#   15 3 * * * /srv/feedback-bot/app/deploy/backup.sh
set -e
BASE=${FEEDBACKBOT_BASE:-/srv/feedback-bot}
DEST=$BASE/backups
STAMP=$(date +%Y%m%d)
mkdir -p "$DEST"
# 用 SQLite 線上備份（機器人執行中也能安全備份）
python3 - "$BASE/data/feedback.db" "$DEST/feedback-$STAMP.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
tar -czf "$DEST/images-$STAMP.tar.gz" -C "$BASE/data" images 2>/dev/null || true
find "$DEST" -type f -mtime +30 -delete
