"""截圖等檔案的儲存：本機資料夾或 Firebase Storage（Cloud Storage）。

存檔後回傳一個參照字串（存進資料庫），之後用同一個字串讀回：
  local:<相對路徑>        本機
  gs://<bucket>/<路徑>    Firebase Storage
"""
from __future__ import annotations

import mimetypes
from pathlib import Path


class LocalFiles:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def save(self, key: str, data: bytes, content_type: str) -> str:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("路徑不合法")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return f"local:{key}"

    def read(self, ref: str) -> tuple[bytes, str] | None:
        if not ref or not ref.startswith("local:"):
            return None
        p = (self.root / ref[len("local:"):]).resolve()
        if self.root not in p.parents or not p.is_file():
            return None
        return p.read_bytes(), mimetypes.guess_type(p.name)[0] or "application/octet-stream"


class GcsFiles:
    def __init__(self, bucket: str, prefix: str = "feedbackbot/", client=None):
        from google.cloud import storage

        self.client = client or storage.Client()
        self.bucket_name = bucket
        self.bucket = self.client.bucket(bucket)
        self.prefix = prefix

    def save(self, key: str, data: bytes, content_type: str) -> str:
        name = self.prefix + key
        self.bucket.blob(name).upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{name}"

    def read(self, ref: str) -> tuple[bytes, str] | None:
        head = f"gs://{self.bucket_name}/"
        if not ref or not ref.startswith(head):
            return None
        blob = self.bucket.blob(ref[len(head):])
        if not blob.exists():
            return None
        data = blob.download_as_bytes()
        return data, blob.content_type or "application/octet-stream"
