#!/bin/sh
# 建立回饋機器人需要的 Firestore 複合索引（只新增，不會動到專案裡其他索引）。
# 用法：sh firebase/firestore-indexes.sh <Firebase 專案 ID> [集合前綴，預設 fbot_]
set -e
PROJECT=${1:?請提供 Firebase 專案 ID}
PREFIX=${2:-fbot_}
C="${PREFIX}messages"
gcloud firestore indexes composite create --project="$PROJECT" --collection-group="$C" --query-scope=collection \
  --field-config=field-path=group_id,order=ascending --field-config=field-path=id,order=descending --async || true
gcloud firestore indexes composite create --project="$PROJECT" --collection-group="$C" --query-scope=collection \
  --field-config=field-path=group_id,order=ascending --field-config=field-path=created_at,order=ascending --async || true
gcloud firestore indexes composite create --project="$PROJECT" --collection-group="$C" --query-scope=collection \
  --field-config=field-path=group_id,order=ascending --field-config=field-path=processed,order=ascending --async || true
echo "索引建立中（約需幾分鐘）。查看狀態：gcloud firestore indexes composite list --project=$PROJECT"
