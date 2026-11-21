# LINE 群組回饋收集機器人

把機器人加進各家公司的 LINE 群組，它會**默默收集**使用者對系統的回饋，每天自動：

```
 白天            晚上 23:30                         你改程式               隔天 09:00
┌────────┐   ┌──────────────────────────┐   ┌──────────────────┐   ┌──────────────────────────┐
│ 群組訊息 │ → │ 1. 萃取回饋（忽略閒聊）      │ → │ /resolve 編號     │ → │ 4. 成效評估報告             │
│ 文字/截圖│   │ 2. 跨公司歸類：共同 vs 單一  │   │  怎麼改 | 在哪裡   │   │ 5. 每個群組各自的修正公告草稿 │
└────────┘   │ 3. AI 分析修改方向、優先級   │   └──────────────────┘   │ 6. 你 /approve → 推播到群組  │
             │ → 日報 + 私訊你摘要          │                          └──────────────────────────┘
             └──────────────────────────┘
```

- **共同問題**：2 家以上公司反映的同一件事（不同說法也會被合併）
- **單一問題**：只有一家公司提出，AI 會提醒你評估是否該做成「可設定選項」，避免影響其他公司
- 每則公告只會講**該群組自己反映過**的問題，不會透露其他公司
- 已公告過的問題不會重複公告；隔天又有人回報同一問題，會歸入原本的問題編號

## 一、建立 LINE 機器人

1. 到 [LINE Developers](https://developers.line.biz/) 建立 Provider → **Messaging API channel**
2. 在 channel 設定：
   - 取得 **Channel secret**（Basic settings）與 **Channel access token**（Messaging API 頁）
   - **Allow bot to join group chats**：開啟
   - **Use webhook**：開啟，Webhook URL 填 `https://你的網域/callback`
3. 在 LINE Official Account Manager 關閉「自動回應訊息」與「加入好友的歡迎訊息」（讓機器人在群組保持安靜）

## 二、安裝與設定

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                       # 填入 LINE 與 Claude 的金鑰
cp system_context.example.md system_context.md   # 寫你系統的模組說明（AI 分析會更準）
```

取得你自己的 userId：先啟動服務，用 LINE 加機器人好友、私訊 `/whoami`，把回覆的 `U...` 填進 `.env` 的 `ADMIN_USER_IDS`（多人用逗號分隔），重新啟動。

## 三、啟動

```bash
python -m feedbackbot serve --port 8000
```

- 需要一個對外的 **HTTPS** 網址（VPS + Nginx、Render、Railway 等；本機測試可用 ngrok）
- 資料存在 `data/feedback.db`（SQLite），部署時請放在**永久磁碟**
- 排程跑在同一個程序內，請只開 **1 個執行個體**

把機器人邀請進各公司群組後，在群組內（用管理者帳號）輸入一次 `/company 公司名稱`。

## 四、管理指令（私訊機器人）

| 指令 | 說明 |
|---|---|
| `/today` | 立即整理目前的回饋（平常每晚自動跑） |
| `/issues` | 未完成問題清單（🔁共同 / 👤單一） |
| `/issue 12` | 問題細節、各公司原話、AI 修改建議 |
| `/start 12` | 標記修改中 |
| `/resolve 12 匯出檔新增出貨日期欄位 \| 訂單管理 > 匯出 Excel` | 標記完成：**怎麼改 \| 在哪裡** |
| `/wontfix 12 原因` | 不處理（不會公告） |
| `/announce` | 立即產生成效評估與公告草稿（平常每天早上自動跑） |
| `/preview` | 看草稿 |
| `/approve` 或 `/approve 3 5` | 發送全部或指定草稿 |
| `/skip 3` | 不發送某草稿 |
| `/groups`、`/company 代碼 公司名` | 群組管理 |

也可以用命令列：`python -m feedbackbot digest | announce | send | issues | issue 12 | resolve 12 "怎麼改" "在哪裡"`

## 五、產出的報告

- `reports/YYYY-MM-DD-digest.md`：回饋日報（共同問題、單一問題、每題的原因分析／建議修改／風險／驗收方式、待辦總表）
  - 這份檔案可以直接丟給 Claude Code 等工具，請它依建議修改你的系統程式碼
- `reports/YYYY-MM-DD-evaluation.md`：修正成效評估（每題怎麼改、改在哪、AI 判斷是否真的回應了使用者需求、後續追蹤）

## 六、設定項（.env）

| 變數 | 預設 | 說明 |
|---|---|---|
| `DAILY_DIGEST_AT` | `23:30` | 每晚整理時間 |
| `DAILY_ANNOUNCE_AT` | `09:00` | 隔天公告準備時間 |
| `AUTO_PUSH` | `false` | `true`＝直接推播；`false`＝先私訊你預覽，`/approve` 才發 |
| `CLAUDE_MODEL` | `claude-opus-5-5` | 分析用的模型 |
| `MAX_IMAGES_PER_GROUP` | `20` | 每群組每天最多判讀幾張截圖 |

## 注意事項

- **告知群組成員**：機器人會記錄群組訊息用於改善系統，建議在群組公告或合約中說明（個資法）。
- **LINE 推播費用**：群組推播是依「群組人數」計算訊息則數，免費方案每月額度有限；回覆管理者指令（reply）不計費。請以 LINE 官方最新方案為準。`AUTO_PUSH=false` 可讓你先審核再發。
- **AI 費用**：每天每個群組 1 次萃取、全部 1 次歸類、每個有變動的問題 1 次分析、每個群組 1 次公告。
- 群組內容一律被當作「資料」交給 AI，不會被當成指令執行；一般成員輸入 `/指令` 不會有作用。
- 機器人只收文字、截圖、檔案名稱；貼圖、語音、影片會忽略。

## 開發

```bash
pip install -r requirements.txt
python -m pytest -q        # 用假的 LINE 與假的 AI 跑完整一天循環
```

程式結構：

```
feedbackbot/
  app.py        Webhook 伺服器、事件處理、排程
  pipeline.py   每日循環：digest / resolve / prepare_announcements / send
  llm.py        Claude 呼叫（萃取、歸類、分析、評估、公告文案）
  commands.py   管理者指令
  db.py         SQLite
  line_api.py   LINE Messaging API
  cli.py        命令列
```
