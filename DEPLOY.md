# 部署到自己的主機（Ubuntu）

> 想用 Firebase（Firestore＋Firebase 網域）請改看 [DEPLOY-FIREBASE.md](DEPLOY-FIREBASE.md)。

以下以 **Ubuntu 22.04 或 24.04** 為例：用 Nginx 處理 HTTPS，用 systemd 讓程式常駐。
可以放在現有的正式機上（另開一個子網域，不影響 Laravel），也可以另開一台小主機（1 核 1GB 就夠）。

> 指令裡的 `feedback.example.com` 都要換成你自己的網域。

---

## 0. 事先準備

| 項目 | 說明 |
|---|---|
| 一台 Ubuntu 主機 | 能用 `sudo` 的帳號登入 |
| 一個子網域 | 例如 `feedback.hokiya.com`，DNS 的 A 記錄指向主機 IP |
| 新的 LINE 頻道 | Channel secret、Channel access token（不要用滿福寶的） |
| Claude API 金鑰 | 到 [Claude Console](https://platform.claude.com/) 建立 |
| Bitbucket 唯讀 Token | Repository settings → Security → Access tokens，只勾「存放庫 → 讀取」。**正式用請把到期日設長一點** |

---

## 1. 安裝系統套件

```bash
sudo apt update
sudo apt install -y python3 python3-venv git nginx certbot python3-certbot-nginx
python3 --version   # 需要 3.10 以上
```

## 2. 建立專用帳號與資料夾

機器人用自己的帳號執行，不和其他服務混在一起。

```bash
sudo useradd --system --create-home --home-dir /srv/feedback-bot --shell /usr/sbin/nologin feedbackbot
sudo chmod 750 /srv/feedback-bot
```

## 3. 下載機器人程式

```bash
sudo -u feedbackbot git clone -b claude/brave-fermi-2srvx5 \
  https://github.com/hanchunhuang/0211.git /srv/feedback-bot/app
```

GitHub 專案如果是私有的，clone 時會要你登入。可以在 GitHub 建一組 Personal access token（只給這個專案的讀取權限），帳號填你的 GitHub 帳號，密碼貼 Token。

## 4. 安裝 Python 套件

```bash
cd /srv/feedback-bot/app
sudo -u feedbackbot python3 -m venv .venv
sudo -u feedbackbot .venv/bin/pip install -r requirements.txt
```

## 5. 下載你系統的程式碼（唯讀）

```bash
sudo -u feedbackbot git clone \
  https://x-token-auth:<Bitbucket唯讀Token>@bitbucket.org/hokiya/line-hokiya-survey.git \
  /srv/feedback-bot/line-hokiya-survey
sudo chmod 700 /srv/feedback-bot/line-hokiya-survey   # Token 存在 .git/config 裡，只讓機器人帳號讀
```

這份程式碼只給機器人分析用，**不要在裡面開發**；機器人每次分析前會自動更新到最新的 master。

## 6. 填寫設定檔

```bash
cd /srv/feedback-bot/app
sudo -u feedbackbot cp .env.example .env
sudo chmod 600 .env
openssl rand -base64 18        # 產生一組後台密碼，複製起來
sudo -u feedbackbot nano .env
```

至少要填這些（**路徑請用絕對路徑**）：

```ini
LINE_CHANNEL_SECRET=新頻道的 secret
LINE_CHANNEL_ACCESS_TOKEN=新頻道的 access token
ANTHROPIC_API_KEY=Claude API 金鑰

DATA_DIR=/srv/feedback-bot/data
REPO_PATH=/srv/feedback-bot/line-hokiya-survey

ADMIN_WEB_PASSWORD=剛剛產生的密碼
```

`ADMIN_USER_IDS` 先空著，第 10 步再填。Telegram、客服人員名單等選填項目見 `.env.example` 的說明。

## 7. 檢查設定

```bash
cd /srv/feedback-bot/app
sudo -u feedbackbot .venv/bin/python -m feedbackbot check
```

LINE、Claude API、程式庫、資料夾都出現 ✅ 才往下做。⚠️ 是提醒，不影響啟動。

## 8. 設成常駐服務

```bash
sudo cp /srv/feedback-bot/app/deploy/feedbackbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now feedbackbot
sudo systemctl status feedbackbot            # 看到 active (running) 就成功
curl http://127.0.0.1:8001/health           # 應回 {"ok":true}
```

看即時紀錄：`sudo journalctl -u feedbackbot -f`

## 9. 設定網域與 HTTPS

```bash
sudo cp /srv/feedback-bot/app/deploy/nginx-feedbackbot.conf /etc/nginx/sites-available/feedbackbot
sudo sed -i 's/feedback.example.com/你的網域/' /etc/nginx/sites-available/feedbackbot
sudo ln -s /etc/nginx/sites-available/feedbackbot /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d 你的網域             # 自動申請 HTTPS 憑證並設定自動更新
curl https://你的網域/health               # 應回 {"ok":true}
```

## 10. 設定 LINE 頻道

到 LINE Developers 的頻道設定：

1. **Messaging API → Webhook URL**：填 `https://你的網域/callback`，按 **Verify**，要顯示 Success。
2. **Use webhook**：開啟。
3. **Allow bot to join group chats**：開啟。
4. LINE Official Account Manager：關閉「自動回應訊息」和「歡迎訊息」。

接著取得你自己的管理者 ID：

1. 用手機加機器人好友，私訊 `/whoami`，複製回覆的 `U...`。
2. 填進 `.env` 的 `ADMIN_USER_IDS`，多人用逗號分隔。
3. 重新啟動：`sudo systemctl restart feedbackbot`
4. 再私訊 `/help`，看到指令清單就代表管理者設定成功。

## 11. 先用一個群組試跑

1. 把機器人邀進**一個**中心的群組，用管理者帳號在群組裡打 `/center 中心名稱`。
2. 開 `https://你的網域/admin` 登入，確認「原始訊息」有收到群組裡的訊息。
3. 私訊機器人 `/today` 立即整理一次，看日報和後台的問題分析準不準。
4. 跑幾天沒問題後，再加進其他中心的群組。

## 12. 每日備份

```bash
sudo -u feedbackbot crontab -e
```

加入這一行（每天 03:15 備份資料庫和截圖，保留 30 天）：

```
15 3 * * * /srv/feedback-bot/app/deploy/backup.sh
```

備份放在 `/srv/feedback-bot/backups/`。建議再定期複製到其他地方（例如 Google Cloud Storage）。

---

## 之後要更新程式

```bash
cd /srv/feedback-bot/app
sudo -u feedbackbot git pull
sudo -u feedbackbot .venv/bin/pip install -r requirements.txt
sudo systemctl restart feedbackbot
```

## 檔案位置總覽

| 路徑 | 內容 |
|---|---|
| `/srv/feedback-bot/app/` | 機器人程式與 `.env` |
| `/srv/feedback-bot/data/feedback.db` | 資料庫（訊息、回饋、問題、公告） |
| `/srv/feedback-bot/data/images/` | 群組截圖 |
| `/srv/feedback-bot/line-hokiya-survey/` | 你系統程式碼的唯讀副本 |
| `/srv/feedback-bot/backups/` | 每日備份 |

## 常見問題

| 狀況 | 原因與處理 |
|---|---|
| LINE 按 Verify 失敗 | `curl https://你的網域/health` 不通 → 檢查 DNS、Nginx、HTTPS。回 400 → `LINE_CHANNEL_SECRET` 填錯 |
| 網頁出現 502 Bad Gateway | 機器人沒在跑：`sudo systemctl status feedbackbot`，看 `journalctl -u feedbackbot -n 50` 的錯誤 |
| 私訊 `/help` 沒反應 | `ADMIN_USER_IDS` 沒填或填錯，改完要 `restart` |
| 後台顯示「後台未啟用」 | `.env` 沒設 `ADMIN_WEB_PASSWORD` |
| 每天沒收到日報 | 看紀錄裡有沒有錯誤；也可以私訊 `/today` 手動觸發 |
| 分析沒有列出程式檔案／commit 沒配對 | 跑 `python -m feedbackbot check` 看程式庫那一項；Bitbucket Token 可能過期 |
| 改了 `.env` 沒生效 | 要 `sudo systemctl restart feedbackbot` |
