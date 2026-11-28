# LINE 群組回饋收集機器人

給「福氣智慧照顧系統」（`hokiya/line-hokiya-survey`）用的回饋收集機器人。

它是一個**獨立的 LINE 機器人**，和客服機器人「滿福寶」一起待在各中心的 LINE 群組裡，但兩者完全分開：
- 滿福寶負責回答操作問題；這個機器人**從不在群組說話**，只默默收集回饋。
- 不修改滿福寶，也不修改系統程式碼。只用一組唯讀 Token clone 程式庫，拿來分析和比對 commit。

## 每天的循環

```
 白天            晚上 23:30                              你照常開發               隔天 09:00
┌────────┐   ┌───────────────────────────────┐   ┌─────────────────────┐   ┌────────────────────────────┐
│ 群組訊息 │ → │ 1. 萃取回饋（忽略閒聊、客服的回答）   │ → │ git commit           │ → │ 4. 讀新 commit，自動配對到問題 │
│ 文字/截圖│   │ 2. 跨中心歸類：共同 vs 單一         │   │ "fix: …… (#12)"      │   │ 5. 成效評估報告               │
└────────┘   │ 3. AI 查程式碼：原因、改哪些檔案、    │   │ push 到 master       │   │ 6. 各群組的修正公告草稿         │
             │    使用者操作位置、優先級            │   └─────────────────────┘   │ 7. 你 /approve → 推播到群組   │
             │ → 日報 + 通知你                    │                             └────────────────────────────┘
             └───────────────────────────────┘
```

- **共同問題**：2 家以上中心反映的同一件事，說法不同也會合併。
- **單一問題**：只有一家中心提出。AI 會評估該做成中心設定（`center_options`）還是直接改預設行為。
- **分析時會查程式碼**：AI 用唯讀工具搜尋與閱讀程式庫，列出要改的檔案和改法，並遵守 `CLAUDE.md` 的規則（PHP 7.4、中心隔離等）。
- **「在哪裡」**：從滿福寶的知識庫（`support_bot_knowledge.json`）查「主選單 → 子選單 → 按鈕」路徑。
- **改完不用手動回報**：每天早上讀取新的 `feat:` / `fix:` commit。AI 判斷有把握解決某個問題，就自動標記完成；只有部分相關的，會列出來請你確認。commit 訊息寫 `#編號` 配對最準。
- 每則公告只寫**該群組自己反映過**的問題，不提其他中心，也不提程式細節。已公告過的不會重複公告。

## 工作方式（預設：手動模式）

1. David 收集各單位群組的訊息，每晚整理成問題：**哪個單位、哪個系統、什麼類型**。
2. 你和夥伴在後台整理問題：**編輯**標題／說明／系統／優先級、指定**負責人**、留**協作備註**、**合併**重複的問題、把回饋**移到**別的問題或拆成新問題。
3. 自己處理程式（例如和 Claude 討論、修改）。問題頁的「問題說明」可以一鍵複製。
4. 處理好後回報給 David，三種方式擇一：
   - **貼上修改回報 md**：後台「系統」→「回報修改」（或問題頁的「📄 貼上修改回報」），貼上或選擇 `.md` 檔，先預覽再套用
   - **LINE 私訊**一則以 `# 修改回報` 開頭的訊息
   - 問題頁直接填「怎麼改的／在哪裡」，或私訊 `/resolve 編號 怎麼改的 | 在哪裡`

   每次回報都會記入**該系統的修改紀錄**（後台「系統」頁），可下載完整紀錄 `.md`，也可以請 David 整理「系統現況摘要」。

   修改回報格式見 `feedbackbot/fixreport.py` 開頭的說明；問題頁的「問題說明」也會附上空白範本。
5. David 每早產生成效評估與公告草稿，你 `/approve` 後通知提出問題的群組。

**單位報表**（後台「單位報表」）：選每天或每月、選單位，列出每個單位**提出了哪些問題**、**解決了哪些問題**，可下載 CSV 用 Excel 開。

手動模式下 David **不讀程式碼**（不分析、不自動配對 commit），AI 額度只用在整理回饋與寫報告。
要開啟讀程式碼的功能，把環境變數 `CODE_ANALYSIS` 設成 `true`。

## 分析什麼時候做（開啟 CODE_ANALYSIS 時）

預設**每晚只歸納**（收集、判斷系統與類型、合併成問題），**不自動讀程式碼分析**。
要知道某個問題怎麼改時，在後台問題頁按「🔍 分析這個問題」，或私訊 David `/analyze 編號`，約 1～3 分鐘後會私訊你結果。
想恢復每晚自動分析，把環境變數 `AUTO_ANALYZE` 設成 `true`。

## 多系統

除了日照系統，也支援其他系統（Hcare、處方箋、兒童之家、健康存摺、藥師雜誌…）。系統清單在 `systems.json`：

- 每筆回饋會判斷屬於哪個系統（優先從該群組勾選的系統中選），只有同系統的回饋會合併成同一個問題
- 分析時查**該系統**的程式碼；commit 也各系統分別配對
- 後台與日報都依系統分類；判斷錯了可在後台問題頁改系統，或私訊 `/system 編號 系統代號`
- 設定方式見 [DEPLOY-FIREBASE.md](DEPLOY-FIREBASE.md) 的「加入其他系統」

## 一、建立新的 LINE 機器人（不要用滿福寶的 channel）

1. [LINE Developers](https://developers.line.biz/) → 新增 **Messaging API channel**，例如命名為「系統回饋小幫手」。
2. 在 channel 設定：
   - 取得 **Channel secret** 和 **Channel access token**
   - **Allow bot to join group chats**：開啟
   - **Use webhook**：開啟，Webhook URL 填 `https://你的網域/callback`
3. 到 LINE Official Account Manager 關閉「自動回應訊息」和「加入好友的歡迎訊息」。

## 二、在主機上準備程式庫（唯讀）

到 Bitbucket 的 Repository settings → Security → Access tokens 建立一組 Token，**只勾「存放庫 → 讀取」**。然後：

```bash
git clone https://x-token-auth:<唯讀Token>@bitbucket.org/hokiya/line-hokiya-survey.git /srv/line-hokiya-survey
```

這個資料夾只給機器人用。機器人每次分析前會 `git fetch` 並 reset 到 `origin/master`，**請不要在裡面開發**。

## 三、安裝與設定

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # 填入 LINE、Claude、REPO_PATH 等
```

- `ADMIN_USER_IDS`：先啟動服務，加機器人好友後私訊 `/whoami` 取得自己的 userId，填入後重新啟動。
- `STAFF_USER_IDS`：在群組回答問題的客服同事。AI 會把他們的話當成背景，不當成回饋。
- `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`：選填。填了的話，每日通知會走 Telegram，不耗 LINE 推播額度。
- `system_context.md`：選填。程式庫裡的文件會自動讀取，這裡只寫程式碼看不出來的事，見 `system_context.example.md`。

## 四、啟動

正式部署有兩種方式，擇一即可：

- **Firebase**（Firestore 資料庫＋Firebase 網域，不用管主機）：看 [DEPLOY-FIREBASE.md](DEPLOY-FIREBASE.md)
- **自己的 Ubuntu 主機**（SQLite＋Nginx＋systemd）：看 [DEPLOY.md](DEPLOY.md)


```bash
python -m feedbackbot serve --port 8000
```

- 需要對外的 **HTTPS** 網址。可以跟 Laravel 放同一台主機，用 Nginx 轉一個子網域或路徑過來。
- 資料存在 `data/feedback.db`（SQLite），請放在永久磁碟。
- 排程跑在同一個程序內，只能開 **1 個執行個體**。

把機器人邀進各中心的群組後，用管理者帳號在每個群組裡輸入一次 `/center 中心名稱`。

## 五、網頁後台

在 `.env` 設定 `ADMIN_WEB_PASSWORD` 後，用瀏覽器開 `https://你的網域/admin`，輸入密碼登入（登入狀態保留 12 小時）。沒設密碼時後台是關閉的。

| 頁面 | 可以看到 |
|---|---|
| **總覽** | 近 7 天新回饋、未完成問題（共同／單一）、已修正但還沒公告的數量；每日新回饋長條圖（7／14／30／90 天，可切換群組）；回饋類型比例；**各群組統計表**（訊息量、回饋數、錯誤／需求／操作不便／疑問、未完成、已修正、最後訊息時間） |
| **單位** | 機器人（例如「福氣工程師 David」）目前負責收集哪些單位：中心名稱、LINE 群組、加入時間、訊息量、回饋與問題數；標出還沒設定中心名稱的群組，以及已離開的群組 |
| **群組頁** | 這個群組反映過的問題（標示是否和其他中心共同）、每筆回饋、原始訊息（標出哪些已被整理成回饋）；可修改中心名稱 |
| **問題** | 依狀態、共同或單一、類型、群組篩選；詳情頁有各中心的原話、AI 分析（含建議修改的檔案）、原始訊息、處理紀錄與 commit；可以直接在網頁上標記完成、修改中或不處理 |
| **原始訊息** | 所有收集到的訊息，可搜尋、可依群組篩選，截圖可以點開 |
| **公告** | 草稿預覽，可在網頁上「發送到群組」或略過；也有發送紀錄 |
| **報告** | 每天的日報與成效評估 |

手機也可以看。後台只有知道密碼的人能進入，並會擋下從其他網站送出的操作（CSRF）。

## 六、管理指令（私訊機器人）

| 指令 | 說明 |
|---|---|
| `/today` | 立即整理回饋（平常每晚自動跑） |
| `/issues` | 未完成問題（🔁 共同 / 👤 單一） |
| `/issue 12` | 細節：各中心原話、原因、建議修改、相關檔案、操作位置、修正 commit |
| `/start 12` | 標記修改中 |
| `/sync` | 立即讀取新 commit 並配對 |
| `/resolve 12 怎麼改的 \| 在哪裡` | 手動標記完成 |
| `/reopen 12` | 取消完成（例如 commit 配錯） |
| `/wontfix 12 原因` | 不處理（不會公告） |
| `/announce` | 立即讀取 commit、產生成效評估和公告草稿（平常每天早上自動跑） |
| `/preview`、`/approve [編號]`、`/skip 編號` | 審核和發送公告 |
| `/groups`、`/center 代碼 中心名稱` | 群組管理 |

命令列也可以操作：`python -m feedbackbot digest | sync | announce | send | issues | issue 12 | resolve 12 "怎麼改" "在哪裡"`

## 七、報告

報告存在資料庫裡，在後台的「報告」頁查看：

- `YYYY-MM-DD-digest.md`：回饋日報，內容包括共同問題、單一問題、原因、建議修改、**建議修改的檔案**、使用者操作位置、風險、驗收方式和待辦總表。可以直接交給 Claude Code，請它依建議修改。
- `YYYY-MM-DD-evaluation.md`：修正成效評估，包括每題怎麼改、改在哪、由哪些 commit 修正，以及 AI 判斷修改是否真的回應了使用者。

## 注意事項

- **告知群組成員**：機器人會記錄群組訊息，用來改善系統。建議在群組公告或合約中說明（個資法）。
- **LINE 推播費用**：推播到群組是按群組人數計算訊息則數；回覆指令（reply）不計費。預設 `AUTO_PUSH=false`，你審核過才發。
- **程式碼安全**：AI 只能用唯讀工具（搜尋、讀檔、看 commit），不能讀 `.env`，也不能讀專案以外的檔案。程式碼片段會送到 Claude API 做分析。
- 群組內容一律當成「資料」交給 AI，不會當成指令執行。一般成員輸入 `/指令` 不會有任何作用。

## 開發

```bash
pip install -r requirements-dev.txt
python -m pytest -q        # 用假的 LINE、假的 AI 和臨時 git 程式庫，跑完整一天的循環
# 也要測 Firestore：先啟動 Firestore 模擬器，再設 FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 跑同一組測試
```

```
feedbackbot/
  app.py        Webhook 伺服器、事件處理、排程、通知（LINE / Telegram）
  admin.py      網頁後台（/admin）
  templates/    後台頁面
  pipeline.py   每日循環：digest / sync_commits / prepare_announcements / send
  llm.py        Claude 呼叫：萃取、歸類、查程式碼分析、commit 配對、評估、公告文案
  repo.py       唯讀存取系統程式庫（git grep / show / log、知識庫查詢）
  commands.py   管理者指令
  db.py         資料存取（問題、回饋、公告…）
  store.py      底層儲存：Firestore 或 SQLite
  files.py      截圖儲存：Firebase Storage 或本機
  line_api.py   LINE Messaging API
  cli.py        命令列
```
