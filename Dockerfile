# Cloud Run 用的映像檔。部署：gcloud run deploy feedbackbot --source .（見 DEPLOY-FIREBASE.md）
FROM python:3.12-slim

# git：讀取你系統的程式碼（分析、commit 配對）
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY feedbackbot ./feedbackbot

# Cloud Run 預設值：Firestore、外部排程、暫存資料夾
ENV PYTHONUNBUFFERED=1 \
    DB_BACKEND=firestore \
    ENABLE_SCHEDULER=false \
    REPO_PATH=/tmp/system-repo \
    DATA_DIR=/tmp/data

CMD ["sh", "-c", "exec uvicorn feedbackbot.app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips='*'"]
