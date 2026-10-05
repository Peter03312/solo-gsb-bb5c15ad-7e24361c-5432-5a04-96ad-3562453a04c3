"""装配版本历史：按版查看、恢复预览与差异演算（纯函数，只读不写库）。

快照的存取由 DB（database.py）提供；本模块负责：
- 由快照数据重建与 GET /api/assembly 同形的装配视图 —— 连通组按快照内的
  有效关系重算，规则与 DB.recompute_groups 一致（孤片 group_id 为 None）；
- 恢复预览的差异演算：目标版（恢复后）与当前版的碎片 / 关系逐项对比，
  changed 条目中 from=当前值、to=恢复后的值。
"""
from __future__ import annotations


# ---------------------------------------------------------------- 装配重建

def compute_group_ids(fragment_ids, relations) -> dict[int, int | None]:
    """按有效关系求连通分量；孤片为 None（与 DB.recompute_groups 同规则）。"""
    parent = {i: i for i in fragment_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for r in relations:
        if not r["active"]:
            continue
        a, b = r["fragment_a"], r["fragment_b"]
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

    comps: dict[int, list[int]] = {}
    for i in fragment_ids:
        comps.setdefault(find(i), []).append(i)
    out: dict[int, int | None] = {}
    for members in comps.values():
        gid = min(members) if len(members) > 1 else None
        for m in members:
            out[m] = gid
    return out


def assembly_from_snapshot(data: dict, version: int) -> dict:
    """把快照数据整理成与 GET /api/assembly 同形的装配视图（不改原数据）。

    连通组按快照内的有效关系重算 —— 与恢复确认时 DB.restore_state 之后的
    recompute_groups 结果一致，保证「预览所见 = 恢复所得」。
    """
    fragments = [dict(f) for f in data["fragments"]]
    relations = [dict(r) for r in data["relations"]]
    gids = compute_group_ids([f["id"] for f in fragments], relations)
    groups: dict[int, list[int]] = {}
    for f in fragments:
        f["group_id"] = gids[f["id"]]
        if gids[f["id"]] is not None:
            groups.setdefault(gids[f["id"]], []).append(f["id"])
    return {
        "version": version,
        "fragments": fragments,
        "groups": [
            {"id": gid, "fragment_ids": sorted(ids)}
            for gid, ids in sorted(groups.items())
        ],
        "relations": relations,
    }


# ---------------------------------------------------------------- 差异演算

def _fragment_changes(cur: dict, tgt: dict) -> dict:
    """同一碎片当前版 → 目标版的内容差异（from=当前，to=恢复后）。"""
    changes: dict = {}
    if cur["name"] != tgt["name"]:
        changes["name"] = {"from": cur["name"], "to": tgt["name"]}
    if cur["contour"] != tgt["contour"]:
        changes["contour"] = {"from_vertices": len(cur["contour"]),
                              "to_vertices": len(tgt["contour"])}
    pose_cur = [cur["x"], cur["y"], cur["theta"]]
    pose_tgt = [tgt["x"], tgt["y"], tgt["theta"]]
    if pose_cur != pose_tgt:
        changes["pose"] = {"from": pose_cur, "to": pose_tgt}
    return changes


def _relation_changes(cur: dict, tgt: dict) -> dict:
    """同一关系当前版 → 目标版的内容差异（from=当前，to=恢复后）。

    只比较业务状态（端点、两侧标记、最大误差、有效状态），不比较
    created_at / undone_at 等时间戳。
    """
    changes: dict = {}
    for field in ("fragment_a", "fragment_b", "markers_a", "markers_b",
                  "max_error", "active"):
        if cur[field] != tgt[field]:
            changes[field] = {"from": cur[field], "to": tgt[field]}
    return changes


def _rel_brief(r: dict) -> dict:
    return {"id": r["id"], "fragment_a": r["fragment_a"],
            "fragment_b": r["fragment_b"], "active": r["active"]}


def diff_assembly(target: dict, current: dict) -> dict:
    """恢复预览差异：target=历史版（恢复后的状态），current=当前版。

    only_in_target：恢复后重新出现的对象；only_in_current：恢复后消失的
    对象；changed：两边都有但内容不同（from=当前，to=恢复后）。
    """
    cur_f = {f["id"]: f for f in current["fragments"]}
    tgt_f = {f["id"]: f for f in target["fragments"]}
    f_only_t = [{"id": i, "name": tgt_f[i]["name"]}
                for i in sorted(tgt_f.keys() - cur_f.keys())]
    f_only_c = [{"id": i, "name": cur_f[i]["name"]}
                for i in sorted(cur_f.keys() - tgt_f.keys())]
    f_changed, f_same = [], 0
    for i in sorted(tgt_f.keys() & cur_f.keys()):
        changes = _fragment_changes(cur_f[i], tgt_f[i])
        if changes:
            f_changed.append({"id": i, "name": cur_f[i]["name"],
                              "changes": changes})
        else:
            f_same += 1

    cur_r = {r["id"]: r for r in current["relations"]}
    tgt_r = {r["id"]: r for r in target["relations"]}
    r_only_t = [_rel_brief(tgt_r[i]) for i in sorted(tgt_r.keys() - cur_r.keys())]
    r_only_c = [_rel_brief(cur_r[i]) for i in sorted(cur_r.keys() - tgt_r.keys())]
    r_changed, r_same = [], 0
    for i in sorted(tgt_r.keys() & cur_r.keys()):
        changes = _relation_changes(cur_r[i], tgt_r[i])
        if changes:
            r_changed.append({"id": i,
                              "fragment_a": tgt_r[i]["fragment_a"],
                              "fragment_b": tgt_r[i]["fragment_b"],
                              "changes": changes})
        else:
            r_same += 1

    identical = not (f_only_t or f_only_c or f_changed
                     or r_only_t or r_only_c or r_changed)
    return {
        "fragments": {
            "only_in_target": f_only_t,
            "only_in_current": f_only_c,
            "changed": f_changed,
            "unchanged_count": f_same,
        },
        "relations": {
            "only_in_target": r_only_t,
            "only_in_current": r_only_c,
            "changed": r_changed,
            "unchanged_count": r_same,
        },
        "summary": {
            "fragments_to_restore": len(f_only_t),
            "fragments_to_remove": len(f_only_c),
            "fragments_to_change": len(f_changed),
            "relations_to_restore": len(r_only_t),
            "relations_to_remove": len(r_only_c),
            "relations_to_change": len(r_changed),
            "identical": identical,
        },
    }
