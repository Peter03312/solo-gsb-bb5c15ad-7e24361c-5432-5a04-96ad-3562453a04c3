"""SQLite 持久化层：碎片、拼接关系、位姿与全局版本号。

位姿 (x, y, theta) 表示碎片局部坐标 → 世界坐标的刚体变换：
    world = R(theta) · local + (x, y)
group_id 为 NULL 表示孤片（独立状态）；同组碎片共享同一 group_id
（取组内最小碎片 id，撤销关系后按剩余关系重算连通分量得到）。
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
        self.conn.commit()
        self.lock = threading.RLock()

    # ------------------------------------------------------------ 版本

    def version(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'version'").fetchone()
        return int(row["value"])

    def bump_version(self):
        v = self.version() + 1
        self.conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'version'", (str(v),))
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
