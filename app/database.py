"""SQLite 持久化层：碎片、拼接关系、位姿、全局版本号与版本历史快照。

位姿 (x, y, theta) 表示碎片局部坐标 → 世界坐标的刚体变换：
    world = R(theta) · local + (x, y)
group_id 为 NULL 表示孤片（独立状态）；同组碎片共享同一 group_id
（取组内最小碎片 id，撤销关系后按剩余关系重算连通分量得到）。

snapshots 表保存版本历史：每次成功变更在提交前（同一事务内）把装配状态
整体写入一条以版本号为主键的不可改快照；功能启用时若尚无快照，先把当前
状态记为基线。快照只增不改 —— 恢复历史版只会在新版本号上留下新快照。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS fragments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    contour    TEXT NOT NULL,           -- JSON [[x, y], ...] 毫米制闭合轮廓
    x          REAL NOT NULL DEFAULT 0,
    y          REAL NOT NULL DEFAULT 0,
    theta      REAL NOT NULL DEFAULT 0,
    group_id   INTEGER,                 -- NULL = 独立碎片
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fragment_a INTEGER NOT NULL,
    fragment_b INTEGER NOT NULL,
    markers_a  TEXT NOT NULL,           -- JSON [[x, y], ...] A 局部坐标
    markers_b  TEXT NOT NULL,           -- JSON [[x, y], ...] B 局部坐标
    max_error  REAL NOT NULL,           -- 采纳时最大对应点误差（毫米）
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    undone_at  TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    version    INTEGER PRIMARY KEY,   -- 装配版本号（快照只增不改）
    data       TEXT NOT NULL,         -- JSON {fragments: [...], relations: [...]}
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('version', '0')")
        # 版本历史启用：尚无快照时把当前状态记为基线快照
        if not self.conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone():
            self.save_snapshot()
        self.conn.commit()
        self.lock = threading.RLock()

    # ------------------------------------------------------------ 版本

    def version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'version'").fetchone()
        return int(row["value"])

    def bump_version(self):
        """递增装配版本，并在同一事务内保存该版的不可改快照。

        每次成功变更（录入/删除/采纳/撤销/试拼确认/修订确认/恢复确认）都会
        经过本方法，因此快照与变更原子生效；预览与失败回滚的操作不留快照。
        """
        v = self.version() + 1
        self.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'version'", (str(v),))
        self.save_snapshot()
        return v

    # ------------------------------------------------------------ 行转换

    @staticmethod
    def fragment_row(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "contour": json.loads(row["contour"]),
            "x": row["x"],
            "y": row["y"],
            "theta": row["theta"],
            "group_id": row["group_id"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def relation_row(row) -> dict:
        return {
            "id": row["id"],
            "fragment_a": row["fragment_a"],
            "fragment_b": row["fragment_b"],
            "markers_a": json.loads(row["markers_a"]),
            "markers_b": json.loads(row["markers_b"]),
            "max_error": row["max_error"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "undone_at": row["undone_at"],
        }

    # ------------------------------------------------------------ 查询

    def get_fragment(self, fid: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM fragments WHERE id = ?", (fid,)).fetchone()
        return self.fragment_row(row) if row else None

    def all_fragments(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM fragments ORDER BY id").fetchall()
        return [self.fragment_row(r) for r in rows]

    def group_members(self, group_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM fragments WHERE group_id = ? ORDER BY id",
            (group_id,)).fetchall()
        return [self.fragment_row(r) for r in rows]

    def all_relations(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM relations ORDER BY id").fetchall()
        return [self.relation_row(r) for r in rows]

    def get_relation(self, rid: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM relations WHERE id = ?", (rid,)).fetchone()
        return self.relation_row(row) if row else None

    # ------------------------------------------------------------ 写入

    def insert_fragment(self, name: str, contour) -> int:
        cur = self.conn.execute(
            "INSERT INTO fragments(name, contour, created_at) VALUES (?, ?, ?)",
            (name, json.dumps(contour), _now()))
        return cur.lastrowid

    def delete_fragment(self, fid: int):
        self.conn.execute(
            "DELETE FROM relations WHERE fragment_a = ? OR fragment_b = ?",
            (fid, fid))
        self.conn.execute("DELETE FROM fragments WHERE id = ?", (fid,))

    def update_pose(self, fid: int, pose):
        self.conn.execute(
            "UPDATE fragments SET x = ?, y = ?, theta = ? WHERE id = ?",
            (pose[0], pose[1], pose[2], fid))

    def update_fragment_contour(self, fid: int, contour):
        """仅更新轮廓；位姿（x/y/theta）、分组保持不变。"""
        self.conn.execute(
            "UPDATE fragments SET contour = ? WHERE id = ?",
            (json.dumps(contour), fid))

    def update_relation_side(self, rid: int, side: str | None, markers,
                             max_error: float):
        """更新关系的边缘标记与最新最大对应点误差。

        side="a"/"b" 时同时替换该侧标记（轮廓修订的本片侧）；side=None 时
        只刷新最大误差（受影响组内不涉及修订片、标记未变的关系）。
        关系 id、两侧碎片归属、active 状态、created_at、undone_at 均不变。
        """
        if side == "a":
            self.conn.execute(
                "UPDATE relations SET markers_a = ?, max_error = ? WHERE id = ?",
                (json.dumps(markers), max_error, rid))
        elif side == "b":
            self.conn.execute(
                "UPDATE relations SET markers_b = ?, max_error = ? WHERE id = ?",
                (json.dumps(markers), max_error, rid))
        else:
            self.conn.execute(
                "UPDATE relations SET max_error = ? WHERE id = ?",
                (max_error, rid))

    def insert_relation(self, fa: int, fb: int, markers_a, markers_b,
                        max_error: float) -> int:
        cur = self.conn.execute(
            "INSERT INTO relations(fragment_a, fragment_b, markers_a, markers_b,"
            " max_error, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (fa, fb, json.dumps(markers_a), json.dumps(markers_b),
             max_error, _now()))
        return cur.lastrowid

    def undo_relation(self, rid: int):
        self.conn.execute(
            "UPDATE relations SET active = 0, undone_at = ? WHERE id = ?",
            (_now(), rid))

    # ------------------------------------------------------------ 连通组

    def recompute_groups(self):
        """按当前有效关系重算连通分量；孤片 group_id 置 NULL（独立状态）。

        只改组归属，不改任何位姿 —— 剩余组内相对位姿保持不变。
        """
        ids = [r["id"] for r in self.conn.execute("SELECT id FROM fragments")]
        parent = {i: i for i in ids}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        for r in self.conn.execute(
                "SELECT fragment_a, fragment_b FROM relations WHERE active = 1"):
            a, b = r["fragment_a"], r["fragment_b"]
            if a in parent and b in parent:
                union(a, b)

        comps: dict[int, list[int]] = {}
        for i in ids:
            comps.setdefault(find(i), []).append(i)
        for members in comps.values():
            gid = min(members) if len(members) > 1 else None
            for m in members:
                self.conn.execute(
                    "UPDATE fragments SET group_id = ? WHERE id = ?", (gid, m))

    # ------------------------------------------------------------ 版本历史快照

    def save_snapshot(self):
        """把当前装配状态整体存为以版本号为主键的不可改快照（须在事务内调用）。

        快照内容 = 全部碎片行（含位姿/分组）+ 全部关系行（含已撤销历史），
        与 GET /api/assembly 所见的完整状态一致。
        """
        data = {"fragments": self.all_fragments(),
                "relations": self.all_relations()}
        self.conn.execute(
            "INSERT INTO snapshots(version, data, created_at) VALUES (?, ?, ?)",
            (self.version(), json.dumps(data), _now()))

    def list_snapshots(self) -> list[dict]:
        """全部快照的摘要（按版本升序），不含快照数据本体。"""
        rows = self.conn.execute(
            "SELECT version, data, created_at FROM snapshots ORDER BY version"
        ).fetchall()
        out = []
        for r in rows:
            data = json.loads(r["data"])
            out.append({
                "version": r["version"],
                "created_at": r["created_at"],
                "fragment_count": len(data["fragments"]),
                "relation_count": len(data["relations"]),
                "active_relation_count":
                    sum(1 for x in data["relations"] if x["active"]),
            })
        return out

    def get_snapshot(self, version: int) -> dict | None:
        """读取某一版的快照数据；不存在返回 None。"""
        row = self.conn.execute(
            "SELECT version, data, created_at FROM snapshots WHERE version = ?",
            (version,)).fetchone()
        if row is None:
            return None
        return {"version": row["version"], "created_at": row["created_at"],
                "data": json.loads(row["data"])}

    def restore_state(self, data):
        """把碎片与关系整体替换为快照内容（原 ID 及状态），并重算连通组。

        不递增版本、不保存新快照 —— 由调用方在同一事务内完成（bump_version）。
        自增序列不受删除影响，后续新建对象不会复用历史 id。
        """
        self.conn.execute("DELETE FROM relations")
        self.conn.execute("DELETE FROM fragments")
        for f in data["fragments"]:
            self.conn.execute(
                "INSERT INTO fragments(id, name, contour, x, y, theta, group_id,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f["id"], f["name"], json.dumps(f["contour"]), f["x"], f["y"],
                 f["theta"], f["group_id"], f["created_at"]))
        for r in data["relations"]:
            self.conn.execute(
                "INSERT INTO relations(id, fragment_a, fragment_b, markers_a,"
                " markers_b, max_error, active, created_at, undone_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r["fragment_a"], r["fragment_b"],
                 json.dumps(r["markers_a"]), json.dumps(r["markers_b"]),
                 r["max_error"], int(r["active"]),
                 r["created_at"], r["undone_at"]))
        self.recompute_groups()

    # ------------------------------------------------------------ 快照

    def assembly_state(self) -> dict:
        fragments = self.all_fragments()
        groups: dict[int, list[int]] = {}
        for f in fragments:
            if f["group_id"] is not None:
                groups.setdefault(f["group_id"], []).append(f["id"])
        return {
            "version": self.version(),
            "fragments": fragments,
            "groups": [
                {"id": gid, "fragment_ids": sorted(ids)}
                for gid, ids in sorted(groups.items())
            ],
            "relations": self.all_relations(),
        }
