"""底層文件儲存：同一套介面，兩種實作。

- FirestoreStore：正式環境（Firebase / Cloud Run）
- SqliteStore：本機開發與測試（單一檔案，不需任何雲端帳號）

上層（db.py）只用這裡的方法，不直接寫 SQL 或 Firestore 查詢。
查詢條件 where 是 (欄位, 運算子, 值) 的串列，運算子支援 == != > >= < <= in。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Protocol

Where = Iterable[tuple[str, str, Any]]
_OPS = {"==": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "in": "IN"}


class DocStore(Protocol):
    def next_id(self, coll: str) -> int: ...
    def set(self, coll: str, doc_id: Any, data: dict) -> None: ...
    def update(self, coll: str, doc_id: Any, fields: dict) -> None: ...
    def get(self, coll: str, doc_id: Any) -> dict | None: ...
    def find(self, coll: str, where: Where = (), order_by: str | None = None, desc: bool = False,
             limit: int | None = None, offset: int = 0, fields: list[str] | None = None) -> list[dict]: ...
    def count(self, coll: str, where: Where = ()) -> int: ...
    def increment(self, coll: str, doc_id: Any, field: str, amount: int = 1) -> None: ...


# ============================================================ SQLite


class SqliteStore:
    """把每份文件存成一列 JSON。用 json_extract 做篩選與排序。"""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS docs (coll TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                                                 PRIMARY KEY (coll, id));
                CREATE TABLE IF NOT EXISTS counters (coll TEXT PRIMARY KEY, n INTEGER NOT NULL);
            """)
            self.conn.commit()

    @staticmethod
    def _path(field: str) -> str:
        if not field.replace("_", "").isalnum():
            raise ValueError(f"欄位名稱不合法：{field}")
        return f"json_extract(data, '$.{field}')"

    def next_id(self, coll: str) -> int:
        with self.lock:
            self.conn.execute("INSERT INTO counters(coll, n) VALUES (?, 1) "
                              "ON CONFLICT(coll) DO UPDATE SET n = n + 1", (coll,))
            n = self.conn.execute("SELECT n FROM counters WHERE coll=?", (coll,)).fetchone()[0]
            self.conn.commit()
            return int(n)

    def set(self, coll: str, doc_id: Any, data: dict) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO docs(coll, id, data) VALUES (?,?,?) "
                              "ON CONFLICT(coll, id) DO UPDATE SET data=excluded.data",
                              (coll, str(doc_id), json.dumps(data, ensure_ascii=False)))
            self.conn.commit()

    def update(self, coll: str, doc_id: Any, fields: dict) -> None:
        with self.lock:
            cur = self.get(coll, doc_id)
            if cur is None:
                raise KeyError(f"{coll}/{doc_id} 不存在")
            cur.update(fields)
            self.set(coll, doc_id, cur)

    def get(self, coll: str, doc_id: Any) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT data FROM docs WHERE coll=? AND id=?", (coll, str(doc_id))).fetchone()
        return json.loads(row[0]) if row else None

    def _where_sql(self, coll: str, where: Where) -> tuple[str, list]:
        sql, params = ["coll=?"], [coll]
        for field, op, value in where:
            if op == "in":
                values = list(value)
                if not values:
                    sql.append("0")
                    continue
                sql.append(f"{self._path(field)} IN ({','.join('?' * len(values))})")
                params += values
            else:
                sql.append(f"{self._path(field)} {_OPS[op]} ?")
                params.append(value)
        return " AND ".join(sql), params

    def find(self, coll: str, where: Where = (), order_by: str | None = None, desc: bool = False,
             limit: int | None = None, offset: int = 0, fields: list[str] | None = None) -> list[dict]:
        w, params = self._where_sql(coll, where)
        sql = f"SELECT data FROM docs WHERE {w}"
        if order_by:
            sql += f" ORDER BY {self._path(order_by)} {'DESC' if desc else 'ASC'}"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        docs = [json.loads(r[0]) for r in rows]
        if fields:
            docs = [{k: d.get(k) for k in fields} for d in docs]
        return docs

    def count(self, coll: str, where: Where = ()) -> int:
        w, params = self._where_sql(coll, where)
        with self.lock:
            return int(self.conn.execute(f"SELECT COUNT(*) FROM docs WHERE {w}", params).fetchone()[0])

    def increment(self, coll: str, doc_id: Any, field: str, amount: int = 1) -> None:
        with self.lock:
            cur = self.get(coll, doc_id) or {}
            self.update(coll, doc_id, {field: int(cur.get(field) or 0) + amount})


# ============================================================ Firestore


class FirestoreStore:
    """Firestore 實作。集合名稱會加上前綴（預設 fbot_），避免和同一個 Firebase 專案的其他資料混在一起。"""

    def __init__(self, project: str | None = None, prefix: str = "fbot_", client=None):
        from google.cloud import firestore  # 只有用 Firestore 時才需要這個套件

        self._fs = firestore
        self.db = client or firestore.Client(project=project or None)
        self.prefix = prefix

    def _c(self, coll: str):
        return self.db.collection(self.prefix + coll)

    def next_id(self, coll: str) -> int:
        fs = self._fs
        ref = self.db.collection(self.prefix + "counters").document(coll)

        @fs.transactional
        def bump(tx):
            snap = ref.get(transaction=tx)
            n = (snap.get("n") if snap.exists else 0) + 1
            tx.set(ref, {"n": n})
            return n

        return int(bump(self.db.transaction()))

    def set(self, coll: str, doc_id: Any, data: dict) -> None:
        self._c(coll).document(str(doc_id)).set(data)

    def update(self, coll: str, doc_id: Any, fields: dict) -> None:
        self._c(coll).document(str(doc_id)).update(fields)

    def get(self, coll: str, doc_id: Any) -> dict | None:
        snap = self._c(coll).document(str(doc_id)).get()
        return snap.to_dict() if snap.exists else None

    def _query(self, coll: str, where: Where):
        from google.cloud.firestore_v1.base_query import FieldFilter

        q = self._c(coll)
        for field, op, value in where:
            q = q.where(filter=FieldFilter(field, op, list(value) if op == "in" else value))
        return q

    def find(self, coll: str, where: Where = (), order_by: str | None = None, desc: bool = False,
             limit: int | None = None, offset: int = 0, fields: list[str] | None = None) -> list[dict]:
        where = list(where)
        # Firestore 的 in 一次最多 30 個值：拆成多次查詢再合併
        for idx, (field, op, value) in enumerate(where):
            values = list(value) if op == "in" else None
            if values is not None and len(values) == 0:
                return []
            if values is not None and len(values) > 30:
                out: list[dict] = []
                for i in range(0, len(values), 30):
                    sub = where[:idx] + [(field, "in", values[i:i + 30])] + where[idx + 1:]
                    out += self.find(coll, sub, fields=fields)
                if order_by:
                    out.sort(key=lambda d: (d.get(order_by) is None, d.get(order_by)), reverse=desc)
                return out[offset: offset + limit if limit is not None else None]
        q = self._query(coll, where)
        if fields:
            q = q.select(fields)
        if order_by:
            q = q.order_by(order_by, direction=self._fs.Query.DESCENDING if desc else self._fs.Query.ASCENDING)
        if offset:
            q = q.offset(offset)
        if limit is not None:
            q = q.limit(limit)
        return [s.to_dict() for s in q.stream()]

    def count(self, coll: str, where: Where = ()) -> int:
        res = self._query(coll, list(where)).count(alias="n").get()
        return int(res[0][0].value) if res and res[0] else 0

    def increment(self, coll: str, doc_id: Any, field: str, amount: int = 1) -> None:
        self._c(coll).document(str(doc_id)).set({field: self._fs.Increment(amount)}, merge=True)
