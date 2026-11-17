"""复核合并持久化：基准、署名意见、裁决历史与合并版本。

- 意见按令牌幂等收件，序号由 SQLite 自增分配，重试不产生重复行；
- 裁决为追加式历史，失效判定在读取时进行，旧裁决保留可追责；
- 合并结果与权重在同一事务写入新版本，按状态摘要幂等复用；
- 全部状态落库，服务中断后重开即可恢复，失败时旧版本不受影响。
"""
import json
import sqlite3
from pathlib import Path
from app.errors import DomainError

SCHEMA = """
CREATE TABLE IF NOT EXISTS review_baselines (
  version TEXT PRIMARY KEY,
  seq INTEGER NOT NULL,
  snapshot TEXT NOT NULL,
  registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_opinions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  token TEXT NOT NULL UNIQUE,
  reviewer TEXT NOT NULL,
  base_version TEXT NOT NULL,
  project_id TEXT NOT NULL,
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_adjudications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  seen_seq INTEGER NOT NULL,
  decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_commits (
  version INTEGER PRIMARY KEY AUTOINCREMENT,
  digest TEXT NOT NULL UNIQUE,
  base_version TEXT NOT NULL,
  result TEXT NOT NULL,
  committed_at TEXT NOT NULL
);
"""


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class ReviewStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    def connect(self):
        return sqlite3.connect(self.path)

    def register_baseline(self, version: str, projects: list, registered_at: str) -> dict:
        raw = _dump(projects)
        with self.connect() as db:
            row = db.execute("SELECT snapshot, seq FROM review_baselines WHERE version=?", (version,)).fetchone()
            if row:
                if row[0] != raw:
                    raise DomainError("DUPLICATE_CONFLICT", "同一基准版本登记了不同内容: " + version)
                return {"version": version, "seq": row[1], "duplicate": True}
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM review_baselines").fetchone()[0]
            db.execute("INSERT INTO review_baselines VALUES (?,?,?,?)", (version, seq, raw, registered_at))
        return {"version": version, "seq": seq, "duplicate": False}

    def find_opinion(self, token: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT seq, token, reviewer, base_version, project_id, field, value, note, received_at"
                " FROM review_opinions WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        return {"seq": row[0], "token": row[1], "reviewer": row[2], "base_version": row[3],
                "project_id": row[4], "field": row[5], "value": json.loads(row[6]),
                "note": row[7], "received_at": row[8]}

    def add_opinion(self, op: dict) -> dict:
        raw = _dump(op["value"])
        with self.connect() as db:
            try:
                cur = db.execute(
                    "INSERT INTO review_opinions(token, reviewer, base_version, project_id, field, value, note, received_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (op["token"], op["reviewer"], op["base_version"], op["project_id"],
                     op["field"], raw, op.get("note", ""), op["received_at"]))
            except sqlite3.IntegrityError:
                raise DomainError("DUPLICATE_CONFLICT", "同一令牌提交了不同内容: " + op["token"])
        return {"seq": cur.lastrowid, "duplicate": False, "received_at": op["received_at"]}

    def add_adjudication(self, adj: dict) -> dict:
        raw = _dump(adj["value"])
        with self.connect() as db:
            row = db.execute(
                "SELECT id, value, decided_by, seen_seq, decided_at FROM review_adjudications"
                " WHERE project_id=? AND field=? ORDER BY id DESC LIMIT 1",
                (adj["project_id"], adj["field"])).fetchone()
            if row and (row[1], row[2], row[3]) == (raw, adj["decided_by"], adj["seen_seq"]):
                return {"id": row[0], "duplicate": True, "decided_at": row[4]}
            cur = db.execute(
                "INSERT INTO review_adjudications(project_id, field, value, decided_by, seen_seq, decided_at)"
                " VALUES (?,?,?,?,?,?)",
                (adj["project_id"], adj["field"], raw, adj["decided_by"], adj["seen_seq"], adj["decided_at"]))
        return {"id": cur.lastrowid, "duplicate": False, "decided_at": adj["decided_at"]}

    def snapshot(self) -> dict:
        with self.connect() as db:
            bases = db.execute("SELECT version, seq, snapshot, registered_at FROM review_baselines ORDER BY seq").fetchall()
            ops = db.execute("SELECT seq, token, reviewer, base_version, project_id, field, value, note, received_at"
                             " FROM review_opinions ORDER BY seq").fetchall()
            adjs = db.execute("SELECT id, project_id, field, value, decided_by, seen_seq, decided_at"
                              " FROM review_adjudications ORDER BY id").fetchall()
        baselines = [{"version": b[0], "seq": b[1], "projects": json.loads(b[2]), "registered_at": b[3]} for b in bases]
        return {
            "baselines": baselines,
            "active_version": baselines[-1]["version"] if baselines else None,
            "opinions": [{"seq": o[0], "token": o[1], "reviewer": o[2], "base_version": o[3],
                          "project_id": o[4], "field": o[5], "value": json.loads(o[6]),
                          "note": o[7], "received_at": o[8]} for o in ops],
            "adjudications": [{"id": a[0], "project_id": a[1], "field": a[2], "value": json.loads(a[3]),
                               "decided_by": a[4], "seen_seq": a[5], "decided_at": a[6]} for a in adjs],
        }

    def commit(self, digest: str, base_version: str, result: dict, committed_at: str) -> dict:
        """合并与权重同事务写入；同一摘要复用原版本，不新增行。"""
        raw = _dump(result)
        with self.connect() as db:
            cur = db.execute("INSERT OR IGNORE INTO review_commits(digest, base_version, result, committed_at)"
                             " VALUES (?,?,?,?)", (digest, base_version, raw, committed_at))
            row = db.execute("SELECT version, committed_at FROM review_commits WHERE digest=?", (digest,)).fetchone()
        return {"version": row[0], "committed_at": row[1], "reused": cur.rowcount == 0}

    def current(self) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT version, digest, base_version, result, committed_at"
                             " FROM review_commits ORDER BY version DESC LIMIT 1").fetchone()
        if not row:
            return None
        return {"version": row[0], "digest": row[1], "base_version": row[2],
                "result": json.loads(row[3]), "committed_at": row[4]}
