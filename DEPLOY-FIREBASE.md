# 部署到 Firebase（Firestore 資料庫＋Firebase 網域）

架構：

```
LINE 群組 ──► https://你的網域/callback ─┐
你（瀏覽器）─► https://你的網域/admin  ───┤ Firebase Hosting（網域、HTTPS）
                                         ▼
                                Cloud Run「feedbackbot」（Python 程式）
                                 │            │             │
                         Firestore（資料）  Storage（截圖）  Bitbucket（唯讀讀程式碼）
                                         ▲
                Cloud Scheduler：每天 23:30 /tasks/digest、09:00 /tasks/announce
```

- 不用自己管主機。程式放在 Cloud Run，沒有請求時會休眠。
- 資料存在 Firestore，集合名稱都以 `fbot_` 開頭，不會和同一個 Firebase 專案的其他資料混在一起。
- 每天的排程由 Cloud Scheduler 叫醒程式執行。

> 以下指令在**你自己的電腦**上執行（macOS 或 Linux 的終端機）。
> `PROJECT_ID` 換成你的 Firebase 專案 ID（Firebase 主控台 → 專案設定可以看到）。

---

## 0. 事先準備

| 項目 | 說明 |
|---|---|
| Firebase 專案 | 用現有的或新建一個都可以 |
| **Blaze 方案（用量計費）** | Cloud Run、Cloud Scheduler、Secret Manager 都需要；要綁信用卡。這個機器人的用量很小 |
| Google Cloud CLI | 安裝：https://cloud.google.com/sdk/docs/install |
| Firebase CLI | `npm install -g firebase-tools` |
| 新的 LINE 頻道 | Channel secret、Channel access token（不要用滿福寶的） |
| Claude API 金鑰 | https://platform.claude.com/ |
| Bitbucket 唯讀 Token | Repository settings → Security → Access tokens，只勾「存放庫 → 讀取」，到期日設長一點 |

下載機器人程式：

```bash
git clone -b claude/brave-fermi-2srvx5 https://github.com/hanchunhuang/0211.git feedback-bot
cd feedback-bot
```

## 1. 登入並設定專案

```bash
gcloud auth login
firebase login
export PROJECT_ID=你的專案ID
export REGION=asia-east1          # 台灣機房
gcloud config set project $PROJECT_ID

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com firestore.googleapis.com
```

## 2. Firestore 資料庫

1. Firebase 主控台 → **Firestore Database**。如果還沒建立就按「建立資料庫」，選**正式版模式**，位置選 `asia-east1 (Taiwan)`。已經有資料庫就直接沿用。
2. 建立機器人需要的索引（只新增，不會動到其他索引）：

```bash
sh firebase/firestore-indexes.sh $PROJECT_ID
```

> ⚠️ **安全規則**：機器人用伺服器身分存取，不受安全規則限制，所以不需要改規則。
> 但請確認專案的規則**不是**「測試模式」（`allow read, write: if true` 這類開放全部的寫法），否則網路上任何人都讀得到 `fbot_` 開頭的資料。

## 3. Firebase Storage（存截圖）

Firebase 主控台 → **Storage**。還沒啟用就按「開始使用」。記下 bucket 名稱，例如 `你的專案ID.firebasestorage.app`：

```bash
export BUCKET=你的專案ID.firebasestorage.app
```

截圖會存在 bucket 裡的 `feedbackbot/` 資料夾。同樣請確認 Storage 規則沒有開放所有人讀取。

## 4. 存放金鑰（Secret Manager）

金鑰不寫進程式也不放在設定檔，改存在 Secret Manager。逐一執行下面的指令；每一行執行後**貼上值、按 Enter**，畫面不會顯示你貼的內容：

```bash
add_secret() { printf '%s 的值：' "$1"; read -rs V; echo; printf '%s' "$V" | gcloud secrets create "$1" --data-file=-; unset V; }

add_secret line-channel-secret        # LINE Channel secret
add_secret line-channel-token         # LINE Channel access token
add_secret anthropic-api-key          # Claude API 金鑰
add_secret admin-web-password         # 後台密碼（自己想一組 12 碼以上）
add_secret repo-url                   # https://x-token-auth:<Bitbucket唯讀Token>@bitbucket.org/hokiya/line-hokiya-survey.git

# 這兩個自動產生
openssl rand -hex 24 | tr -d '\n' | gcloud secrets create task-token --data-file=-
openssl rand -hex 32 | tr -d '\n' | gcloud secrets create session-secret --data-file=-
```

讓 Cloud Run 可以讀金鑰、Firestore 和 Storage：

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
SA=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com

for s in line-channel-secret line-channel-token anthropic-api-key admin-web-password repo-url task-token session-secret; do
  gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor --quiet >/dev/null
done
gcloud projects add-iam-policy-binding $PROJECT_ID --member=serviceAccount:$SA --role=roles/datastore.user --quiet >/dev/null
gcloud storage buckets add-iam-policy-binding gs://$BUCKET --member=serviceAccount:$SA --role=roles/storage.objectAdmin --quiet >/dev/null
```

## 5. 部署程式到 Cloud Run

在 `feedback-bot` 資料夾執行（第一次大約 3～5 分鐘）：

```bash
gcloud run deploy feedbackbot --source . --region $REGION \
  --allow-unauthenticated \
  --no-cpu-throttling --min-instances 0 --max-instances 1 \
  --timeout 3600 --memory 1Gi \
  --set-env-vars "FIREBASE_PROJECT_ID=$PROJECT_ID,FIREBASE_STORAGE_BUCKET=$BUCKET,TIMEZONE=Asia/Taipei,AUTO_PUSH=false" \
  --set-secrets "LINE_CHANNEL_SECRET=line-channel-secret:latest,LINE_CHANNEL_ACCESS_TOKEN=line-channel-token:latest,ANTHROPIC_API_KEY=anthropic-api-key:latest,ADMIN_WEB_PASSWORD=admin-web-password:latest,REPO_URL=repo-url:latest,TASK_TOKEN=task-token:latest,SESSION_SECRET=session-secret:latest"

RUN_URL=$(gcloud run services describe feedbackbot --region $REGION --format='value(status.url)')
curl $RUN_URL/health     # 應回 {"ok":true}
```

參數說明：

- `--allow-unauthenticated`：LINE 和 Firebase Hosting 要能連進來。程式本身會驗證 LINE 簽章、後台密碼和排程密鑰。
- `--no-cpu-throttling`：回覆 LINE 之後，背景工作（例如存截圖、`/today`）才能繼續跑完。
- `--max-instances 1`：只開一份程式，避免排程重複執行。

如果出現「組織政策不允許 allUsers」的錯誤，代表公司的 Google Cloud 有限制，需要請管理員開放。

## 6. 設定 Firebase 網域

```bash
firebase use $PROJECT_ID
firebase deploy --only hosting
curl https://$PROJECT_ID.web.app/health     # 應回 {"ok":true}
```

這會把 `firebase.json` 裡的設定部署上去，所有網址都轉給 Cloud Run 的 `feedbackbot`。

- 現在就能用 `https://你的專案ID.web.app`。
- **自訂網域**（例如 `feedback.hokiya.com`）：Firebase 主控台 → Hosting → 新增自訂網域，照畫面到網域商加 DNS 記錄。HTTPS 憑證會自動申請，可能要等幾小時。

設定好網址後告訴程式（換成你實際用的網域）：

```bash
gcloud run services update feedbackbot --region $REGION --update-env-vars PUBLIC_BASE_URL=https://feedback.hokiya.com
```

> 如果 `firebase deploy` 說不支援 `asia-east1`：把步驟 5 的 `REGION` 改成 `asia-northeast1` 重新部署，`firebase.json` 裡的 `region` 也改成一樣的值。

## 7. 每日排程（Cloud Scheduler）

```bash
TOKEN=$(gcloud secrets versions access latest --secret=task-token)

gcloud scheduler jobs create http feedbackbot-digest --location $REGION \
  --schedule "30 23 * * *" --time-zone "Asia/Taipei" \
  --uri "$RUN_URL/tasks/digest" --http-method POST --headers "X-Task-Token=$TOKEN" --attempt-deadline 1800s

gcloud scheduler jobs create http feedbackbot-announce --location $REGION \
  --schedule "0 9 * * *" --time-zone "Asia/Taipei" \
  --uri "$RUN_URL/tasks/announce" --http-method POST --headers "X-Task-Token=$TOKEN" --attempt-deadline 1800s

unset TOKEN
```

- 每晚 23:30 整理回饋。
- 每天 09:00 讀取 commit、產生成效評估和公告草稿。
- 想手動跑一次：`gcloud scheduler jobs run feedbackbot-digest --location $REGION`

## 8. 設定 LINE 頻道

LINE Developers 的頻道設定：

1. **Webhook URL**：`https://你的網域/callback`，按 **Verify** 要顯示 Success。
2. **Use webhook** 開啟；**Allow bot to join group chats** 開啟。
3. LINE Official Account Manager：關閉「自動回應訊息」和「歡迎訊息」。

取得管理者 ID：

1. 加機器人好友，私訊 `/whoami`，複製回覆的 `U...`。
2. 執行下面的指令：

```bash
gcloud run services update feedbackbot --region $REGION --update-env-vars ADMIN_USER_IDS=U你的ID
# 多人用逗號分隔時要換分隔符號：--update-env-vars "^@^ADMIN_USER_IDS=U111,U222"
```

3. 再私訊 `/help`，看到指令清單就成功了。

客服同事的 ID（`STAFF_USER_IDS`）、Telegram 通知等選填設定，也用同樣的方式加入。

## 9. 先用一個群組試跑

1. 把機器人邀進**一個**中心的群組，用管理者帳號在群組裡打 `/center 中心名稱`。
2. 開 `https://你的網域/admin` 登入，在「原始訊息」確認有收到訊息。
3. 私訊機器人 `/today` 立即整理一次，看日報和問題分析準不準。
4. 跑幾天沒問題後，再加進其他中心的群組。

---

## 加入其他系統（多系統）

機器人可以同時服務多套系統。每筆回饋會先判斷屬於**哪一個系統**，再到**那個系統的程式碼**分析怎麼改。

系統清單寫在 `systems.json`（代號、名稱、說明、Bitbucket repo），裡面不放任何密碼。
**說明寫得越清楚（主要功能、常見用詞），AI 越能判斷正確。**

每個系統的程式碼讀取權杖，要分別存成金鑰：

**① 每個 repo 各建一組唯讀權杖**

到該 repo 的 Repository settings → Security → Access tokens → 建立，只勾「存放庫 → 讀取」，**只複製權杖本身**（不用組網址）。
`becca0330` 底下的 repo（hcare、pd-ai-system），要請該 repo 的管理者（Becca）建立。

**② 存成金鑰**（每一行貼上對應 repo 的權杖）

```bash
add_secret() { printf '%s 的值：' "$1"; read -rs V; echo; printf '%s' "$V" | gcloud secrets create "$1" --data-file=-; unset V; }
add_secret repo-token-hcare
add_secret repo-token-pd-ai
add_secret repo-token-children-care
add_secret repo-token-health-passbook
add_secret repo-token-pharma-journal
```

**③ 讓程式讀得到這些金鑰**

```bash
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
SA=${PROJECT_NUMBER}-compute@developer.gserviceaccount.com
for s in repo-token-hcare repo-token-pd-ai repo-token-children-care repo-token-health-passbook repo-token-pharma-journal; do
  gcloud secrets add-iam-policy-binding $s --member=serviceAccount:$SA --role=roles/secretmanager.secretAccessor --quiet >/dev/null && echo "✅ $s"
done
```

**④ 部署新版程式，並接上這些金鑰**

```bash
cd ~/feedback-bot && git pull
gcloud run deploy feedbackbot --source . --region $REGION \
  --update-secrets "REPO_TOKEN_HCARE=repo-token-hcare:latest,REPO_TOKEN_PD_AI=repo-token-pd-ai:latest,REPO_TOKEN_CHILDREN_CARE=repo-token-children-care:latest,REPO_TOKEN_HEALTH_PASSBOOK=repo-token-health-passbook:latest,REPO_TOKEN_PHARMA_JOURNAL=repo-token-pharma-journal:latest"
```

日照系統沿用原本的 `repo-url`，不用改。

**⑤ 在後台設定每個群組用哪些系統**

後台「單位」→ 點群組 → 勾選這個單位使用的系統 →「儲存系統」。
例如「南老兒童之家」群組勾「兒童之家系統」。都不勾的話，AI 會從全部系統中判斷。

**⑥ 確認**

私訊機器人 `/sync`。回覆中如果出現「⚠️ 讀不到程式碼、略過：…」，代表那幾個系統的權杖有問題。
還沒有權杖的系統也能收集與分類回饋，只是分析時不會查程式碼，也不會自動配對 commit。

## 之後要更新程式

```bash
cd feedback-bot && git pull
gcloud run deploy feedbackbot --source . --region $REGION     # 原本的設定和金鑰會保留
```

## 更換金鑰（例如 Bitbucket Token 到期）

```bash
printf '%s' 'https://x-token-auth:<新Token>@bitbucket.org/hokiya/line-hokiya-survey.git' | gcloud secrets versions add repo-url --data-file=-
gcloud run services update feedbackbot --region $REGION --update-labels rotated=$(date +%s)   # 重新啟動以讀取新值
```

## 備份

Firestore 可以設定每日自動備份（保留 14 週）：

```bash
gcloud firestore backups schedules create --database='(default)' --recurrence=daily --retention=14w
```

## 資料在哪裡

| 位置 | 內容 |
|---|---|
| Firestore `fbot_groups` | 群組 |
| Firestore `fbot_messages` | 收集到的訊息 |
| Firestore `fbot_feedback`、`fbot_issues` | 回饋、問題與 AI 分析 |
| Firestore `fbot_announcements`、`fbot_reports` | 公告、日報與成效評估 |
| Storage `feedbackbot/images/` | 群組截圖 |

## 常見問題

| 狀況 | 原因與處理 |
|---|---|
| LINE 按 Verify 失敗 | `curl https://你的網域/health` 不通 → 檢查 Hosting 是否部署、自訂網域憑證是否完成。回 400 → LINE secret 填錯 |
| 看錯誤紀錄 | Google Cloud 主控台 → Cloud Run → feedbackbot → 記錄；或 `gcloud run services logs read feedbackbot --region $REGION --limit 50` |
| 後台出現 `The query requires an index` | 步驟 2 的索引還沒建好（要幾分鐘）；或 `FIRESTORE_PREFIX` 改過，要用新前綴重跑索引腳本 |
| 後台登入後又跳回登入頁 | `PUBLIC_BASE_URL` 要和瀏覽器網址列的網域一致（包含 https://） |
| 排程沒跑 | Cloud Scheduler 頁面看執行紀錄；403 代表 `X-Task-Token` 和 `task-token` 金鑰不一致 |
| 分析沒有列出程式檔案 | Bitbucket Token 過期或無權限，照上面「更換金鑰」換新 |
