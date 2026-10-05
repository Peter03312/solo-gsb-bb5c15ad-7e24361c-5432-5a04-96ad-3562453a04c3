"""陶片拼接系统 —— FastAPI 后端。

业务规则：
- 录入毫米制闭合轮廓，自交轮廓整次拒绝；
- 候选拼接需两片各至少 2 处对应边缘标记，标记重合拒绝；
- 采纳拼接仅做平移+旋转：同组对应点误差均 ≤ 1mm，同组轮廓内部不重叠；
- 合并两组时整体移动其中一组，组内相对位姿不变；
- 同组内新增关系（闭环）必须符合已有位姿；
- 任何校验失败整次拒绝，原拼接组不变；
- 撤销关系后按剩余关系重算连通组，孤片恢复独立状态；
- 采纳/撤销携带期望版本号，过期版本返回 409 冲突；
- 有序试拼（/api/trials/rehearse）基于版本快照逐步推演，不落库；
  确认（/api/trials/commit）整体提交，任一步失败或版本过期均不写入，
  成功仅递增一次装配版本（详见 trial.py）。
- 轮廓修订（/api/fragments/{id}/revision/preview|confirm）：提交修订后的
  毫米制闭合轮廓及该片每条有效关系的新边缘标记，以现有位姿校验受影响组
  误差与碰撞；预览不落库，确认时重新校验同一输入并原子更新，位姿、关系
  ID、分组、已撤销历史不变，装配版本只 +1（详见 revision.py）。
"""
from __future__ import annotations

import math
import os
from contextlib import asynccontextmanager
from typing import Literal, Union

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import geometry, revision, trial
from .database import DB
from .revision import RevisionError, evaluate_revision
from .trial import TrialError, evaluate_proposal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get(
    "DATABASE_PATH", os.path.join(BASE_DIR, "..", "data", "app.db"))
SEED_DEMO_DATA = os.environ.get("SEED_DEMO_DATA", "1") != "0"

db: DB | None = None


def get_db() -> DB:
    assert db is not None
    return db


# ---------------------------------------------------------------- 请求模型

class FragmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    contour: list[list[float]] = Field(min_length=3)


class PreviewIn(BaseModel):
    fragment_a: int
    fragment_b: int
    markers_a: list[list[float]] = Field(default_factory=list)
    markers_b: list[list[float]] = Field(default_factory=list)


class AcceptIn(PreviewIn):
    expected_version: int


class UndoIn(BaseModel):
    expected_version: int


class TrialRelateStep(BaseModel):
    action: Literal["relate"] = "relate"
    fragment_a: int
    fragment_b: int
    markers_a: list[list[float]] = Field(default_factory=list)
    markers_b: list[list[float]] = Field(default_factory=list)
    # 可选：供序列内后续撤销步引用本步新建的关系
    client_id: str | None = Field(default=None, max_length=100)


class TrialUndoStep(BaseModel):
    action: Literal["undo"] = "undo"
    relation_id: int | None = None       # 撤销已存在的有效关系
    client_id: str | None = None         # 或撤销序列内前序新建关系


TrialStep = Union[TrialRelateStep, TrialUndoStep]


class TrialIn(BaseModel):
    expected_version: int
    steps: list[TrialStep] = Field(default_factory=list)


class RevisionRelationIn(BaseModel):
    relation_id: int
    markers: list[list[float]] = Field(default_factory=list)


class RevisionIn(BaseModel):
    contour: list[list[float]] = Field(min_length=3)
    # 本片每一条有效关系的新边缘标记（遗漏有效关系会被整次拒绝）
    relations: list[RevisionRelationIn] = Field(default_factory=list)
    expected_version: int


# ---------------------------------------------------------------- 拼接演算

def _load_pair(d: DB, fa_id: int, fb_id: int):
    if fa_id == fb_id:
        raise HTTPException(400, "不能与同一片陶片建立拼接关系")
    fa, fb = d.get_fragment(fa_id), d.get_fragment(fb_id)
    if fa is None or fb is None:
        missing = fa_id if fa is None else fb_id
        raise HTTPException(404, f"碎片 #{missing} 不存在")
    return fa, fb


# 位姿方案演算 evaluate_proposal 位于 trial.py，单条预览/采纳与试拼共用。


def _check_version(d: DB, expected: int):
    current = d.version()
    if expected != current:
        raise HTTPException(
            409, f"版本冲突：当前数据版本为 v{current}，"
                 f"您基于 v{expected} 的操作已过期，请刷新后重试")


# ---------------------------------------------------------------- 应用生命周期

def seed_demo(d: DB):
    """初始化演示数据（仅当库为空且未禁用）。"""
    if d.all_fragments():
        return
    demo = [
        ("陶片A-左半", [[0, 0], [35, 0], [38, 15], [33, 30], [37, 45], [34, 60], [0, 60]]),
        ("陶片B-右半", [[1, 0], [4, 15], [-1, 30], [3, 45], [0, 60], [46, 60], [46, 0]]),
        ("陶片C", [[0, 0], [40, 0], [35, 25], [10, 30]]),
        ("陶片D", [[0, 0], [30, 5], [25, 35], [-5, 20]]),
    ]
    with d.lock:
        for name, contour in demo:
            d.insert_fragment(name, contour)
        d.bump_version()
        d.conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db
    db = DB(DB_PATH)
    if SEED_DEMO_DATA:
        seed_demo(db)
    yield
    db.conn.close()


app = FastAPI(title="陶片拼接系统", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------- 碎片 API

@app.post("/api/fragments", status_code=201)
def create_fragment(payload: FragmentIn):
    d = get_db()
    try:
        contour = geometry.validate_contour(payload.contour)
    except geometry.GeometryError as e:
        raise HTTPException(400, str(e))
    with d.lock:
        fid = d.insert_fragment(payload.name.strip(), contour)
        d.bump_version()
        d.conn.commit()
        return {"ok": True, "fragment": d.get_fragment(fid),
                "version": d.version()}


@app.get("/api/fragments")
def list_fragments():
    return get_db().all_fragments()


@app.delete("/api/fragments/{fid}")
def delete_fragment(fid: int, expected_version: int):
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, expected_version)
            if d.get_fragment(fid) is None:
                raise HTTPException(404, f"碎片 #{fid} 不存在")
            d.delete_fragment(fid)
            d.recompute_groups()
            d.bump_version()
            d.conn.commit()
        except Exception:
            d.conn.rollback()
            raise
    return {"ok": True, "assembly": d.assembly_state()}


# ---------------------------------------------------------------- 轮廓修订 API

def _revision_response(fid: int, expected_version: int, ev: dict) -> dict:
    """把 evaluate_revision 结果整理为预览/确认共用的响应体（不落库语义）。"""
    rel_rows = []
    for row in ev["relations"]:
        rel_rows.append({
            "relation_id": row["relation_id"],
            "revised": row["revised"],
            "side": row["side"],
            "fragment_a": row["fragment_a"],
            "fragment_b": row["fragment_b"],
            "other_fragment_id": row["other_fragment_id"],
            "required_count": row["required_count"],
            "marker_count": row["marker_count"],
            "errors": [round(e, 4) for e in row["errors"]],
            "max_error_mm": (round(row["max_error_mm"], 4)
                             if row["max_error_mm"] is not None else None),
            "conflicts": row["conflicts"],
            "markers_a": row["markers_a"],
            "markers_b": row["markers_b"],
        })
    return {
        "ok": ev["ok"],
        "fragment_id": fid,
        "expected_version": expected_version,
        "independent": ev["independent"],
        "missing_relation_ids": ev["missing_relation_ids"],
        "contour": [list(p) for p in ev["contour"]] if ev["contour"] else None,
        "conflicts": ev["conflicts"],
        "relations": rel_rows,
        "overlaps": [
            {"fragment_a": o["fragment_a"], "fragment_b": o["fragment_b"],
             "name_a": o["name_a"], "name_b": o["name_b"],
             "area_mm2": round(o["area_mm2"], 6)}
            for o in ev["overlaps"]
        ],
        "involved_fragment_ids": ev["involved"],
    }


@app.post("/api/fragments/{fid}/revision/preview")
def preview_revision(fid: int, payload: RevisionIn):
    """预览轮廓修订：逐条误差与冲突原因，不写库。版本过期返回 409。"""
    d = get_db()
    with d.lock:
        _check_version(d, payload.expected_version)
        if d.get_fragment(fid) is None:
            raise HTTPException(404, f"碎片 #{fid} 不存在")
        rel_inputs = [(r.relation_id, r.markers) for r in payload.relations]
        try:
            ev = evaluate_revision(d, fid, payload.contour, rel_inputs)
        except RevisionError as e:
            raise HTTPException(e.status, e.detail())
        return _revision_response(fid, payload.expected_version, ev)


@app.post("/api/fragments/{fid}/revision/confirm", status_code=201)
def confirm_revision(fid: int, payload: RevisionIn):
    """确认轮廓修订：重新校验同一输入，原子更新轮廓/相关标记/误差。

    位姿、关系 ID、连通分组、已撤销历史保持不变；成功后装配版本只 +1。
    校验失败整次拒绝（不写入任何变化）；版本过期返回 409。
    """
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            if d.get_fragment(fid) is None:
                raise HTTPException(404, f"碎片 #{fid} 不存在")
            rel_inputs = [(r.relation_id, r.markers) for r in payload.relations]
            try:
                ev = evaluate_revision(d, fid, payload.contour, rel_inputs)
            except RevisionError as e:
                raise HTTPException(e.status, e.detail())
            if not ev["ok"]:
                raise HTTPException(400, detail={
                    "message": "轮廓修订被拒绝，未写入任何变化",
                    **_revision_response(fid, payload.expected_version, ev),
                })
            d.update_fragment_contour(fid, ev["contour"])
            for rid, up in ev["updates"].items():
                d.update_relation_side(rid, up["side"], up["markers"],
                                       up["max_error"])
            # 不调用 recompute_groups：分组必须保持不变
            new_version = d.bump_version()
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "fragment_id": fid, "version": new_version,
            "assembly": d.assembly_state()}


# ---------------------------------------------------------------- 拼接 API

@app.post("/api/relations/preview")
def preview_relation(payload: PreviewIn):
    """预览：仅平移+旋转后的组合，返回各片位置、角度及冲突原因，不落库。"""
    d = get_db()
    with d.lock:
        fa, fb = _load_pair(d, payload.fragment_a, payload.fragment_b)
        ev = evaluate_proposal(d, fa, fb, payload.markers_a, payload.markers_b)
        if not ev["involved"]:
            return {"ok": False, "relation_type": None,
                    "conflicts": ev["conflicts"], "errors": [],
                    "max_error_mm": None, "placements": [], "markers_world": None}
        placements = []
        for fid in ev["involved"]:
            f = ev["by_id"][fid]
            x, y, th = ev["poses"][fid]
            placements.append({
                "fragment_id": fid,
                "name": f["name"],
                "x": round(x, 4),
                "y": round(y, 4),
                "theta_deg": round(math.degrees(th), 4),
                "contour_world": geometry.contour_world(f["contour"], (x, y, th)),
            })
        ma, mb = ev["markers"]
        pa, pb = ev["poses"][fa["id"]], ev["poses"][fb["id"]]
        return {
            "ok": ev["ok"],
            "relation_type": ev["relation_type"],
            "conflicts": ev["conflicts"],
            "errors": [round(e, 4) for e in ev["errors"]],
            "max_error_mm": round(max(ev["errors"]), 4) if ev["errors"] else None,
            "placements": placements,
            "markers_world": {
                "a": [geometry.apply_pose(m, pa) for m in ma],
                "b": [geometry.apply_pose(m, pb) for m in mb],
            },
        }


@app.post("/api/relations", status_code=201)
def accept_relation(payload: AcceptIn):
    """采纳拼接：全部校验通过才落库，任一失败整次拒绝、原拼接组不变。"""
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            fa, fb = _load_pair(d, payload.fragment_a, payload.fragment_b)
            ev = evaluate_proposal(d, fa, fb, payload.markers_a, payload.markers_b)
            if not ev["ok"]:
                raise HTTPException(400, detail={
                    "message": "拼接被拒绝，原拼接组不变",
                    "conflicts": ev["conflicts"],
                })
            for fid, pose in ev["new_poses"].items():
                d.update_pose(fid, pose)
            rid = d.insert_relation(fa["id"], fb["id"], ev["markers"][0],
                                    ev["markers"][1], max(ev["errors"]))
            d.recompute_groups()
            d.bump_version()
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "relation_id": rid, "assembly": d.assembly_state()}


@app.post("/api/relations/{rid}/undo")
def undo_relation(rid: int, payload: UndoIn):
    """撤销关系：按剩余关系重算连通组，孤片恢复独立状态。"""
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            rel = d.get_relation(rid)
            if rel is None:
                raise HTTPException(404, f"拼接关系 #{rid} 不存在")
            if not rel["active"]:
                raise HTTPException(
                    409, f"版本冲突：拼接关系 #{rid} 已被撤销，请刷新后重试")
            d.undo_relation(rid)
            d.recompute_groups()
            d.bump_version()
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "assembly": d.assembly_state()}


@app.get("/api/relations")
def list_relations():
    return get_db().all_relations()


@app.get("/api/assembly")
def get_assembly():
    """当前装配快照：版本号、碎片位姿、连通组、全部关系。"""
    return get_db().assembly_state()


# ---------------------------------------------------------------- 有序试拼

def _rehearse_response(sim: dict) -> dict:
    """把推演结果整理成 /trials/rehearse 响应（不含内部 actions）。"""
    return {
        "ok": sim["ok"],
        "base_version": sim["base_version"],
        "steps": sim["steps"],
        "failed_at": sim["failed_at"],
        "failure": sim["failure"],
        "assembly": sim["assembly"],
    }


@app.post("/api/trials/rehearse")
def rehearse_trial(payload: TrialIn):
    """试拼预演：基于 expected_version 快照逐步推演，不改数据。

    返回每步结果（位姿/误差/连通组）、失败步及失败原因，以及最终（或失败
    前一刻）的位姿与分组。版本过期返回 409。
    """
    d = get_db()
    with d.lock:
        _check_version(d, payload.expected_version)
        try:
            sim = trial.simulate(d, payload.steps)
        except TrialError as e:
            raise HTTPException(e.status, e.detail())
    return _rehearse_response(sim)


@app.post("/api/trials/commit", status_code=201)
def commit_trial(payload: TrialIn):
    """确认试拼：同一版本 + 同一操作序列整体提交。

    任一步失败或版本过期均不写入任何变化；全部成功仅递增一次装配版本。
    """
    d = get_db()
    with d.lock:
        try:
            d.conn.execute("BEGIN IMMEDIATE")
            _check_version(d, payload.expected_version)
            try:
                sim = trial.simulate(d, payload.steps)
            except TrialError as e:
                raise HTTPException(e.status, e.detail())
            if not sim["ok"]:
                raise HTTPException(
                    sim["failure"]["http_status"], detail={
                        "message": "试拼序列存在失败步骤，未写入任何变化",
                        "failed_at": sim["failed_at"],
                        **{k: v for k, v in sim["failure"].items()
                           if k != "http_status"},
                    })
            cid_map = trial.commit_actions(d, sim)
            new_version = d.bump_version()
            d.conn.commit()
        except HTTPException:
            d.conn.rollback()
            raise
        except Exception:
            d.conn.rollback()
            raise HTTPException(500, "内部错误，本次操作未生效")
    return {"ok": True, "version": new_version, "committed_steps": len(sim["steps"]),
            "relation_ids": cid_map, "assembly": d.assembly_state()}


# ---------------------------------------------------------------- 前端静态页

app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True),
          name="static")
