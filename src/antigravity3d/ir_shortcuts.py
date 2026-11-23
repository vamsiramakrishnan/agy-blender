"""Mirror and repeat shortcuts of the model-facing node, expanded into plain nodes when an answer is parsed.

Most props are symmetric and repeat parts (four corner guards, two handles, six bolts). Written out, every instance is a
chance to get one slightly wrong; with a shortcut the author writes one and the copies are exact. The shortcuts exist
only on the wire (``ir_schema``): ``scene_from_wire`` / ``patch_from_wire`` call :func:`expand`, so ``ir.SceneIR``, the
kernel and everything after it, a repair request included, see plain nodes with ids.

Rules (a node dict here is an ``ir.NodeIR`` dump plus the shortcut keys):

* ``mirror`` ("x" | "y" | "z"): the node and its subtree, and also their reflection through the plane perpendicular to
  that axis of the parent's frame, through the parent's origin (the asset's origin for a root node). The part (its
  whole subtree, each row or ring copy) must lie on one side of that plane, touching it at most: one that straddles it
  would be copied onto itself (``_one_side``).
* ``repeat``: ``{"count": n, "offset": [dx, dy, dz]}`` (a row: instance k is moved by k * offset, in the parent's
  frame) or ``{"count": n, "around": "x" | "y" | "z"}`` (a ring: instance k is turned by 360 * k / n degrees about that
  axis of the parent's frame, through its origin); n (2..``MAX_REPEAT``) counts the original. One field with two
  shapes rather than three flat fields: live (2026-11-22, worker tier) one answer in three wrote repeat_count with both
  repeat_offset and repeat_around, which the flat schema allowed and the parse refused, so the candidate was lost;
  each shape forbids the other's key, so constrained decoding cannot write that state.
* A node's subtree (the nodes whose parent chain leads to it) is copied with it; a child's own shortcuts are expanded
  first, in its parent's frame. On one node repeat comes before mirror: the mirror reflects the whole row or ring.
* Ids: the original keeps its id; a repeat copy appends ``__r<k>``, a mirror copy ``__m<axis>`` (``bolt__r2__mx``),
  and the nodes under a copied root are named inside it (``handle__mx__grip``), parents following. A copy whose id
  exists, or is longer than 64 characters, is an error.
* After parsing only these expanded nodes exist: the shortcut is gone, a patch or repair addresses the expanded ids,
  and editing one copy edits that node only (copies do not follow later edits of the original). A patch's own
  upsert_node may carry a shortcut again; it expands over the nodes upserted in that patch.
* Output order: the original nodes in their input order, then the copies in the order they were made, so a scene
  without shortcuts comes back unchanged.

A reflection is written without a negative scale: for the parent-frame reflection R and the node's linear part
Rot * S, R * Rot * S = (R * Rot * L) * S * L, where L is a reflection of the node's own frame along one axis b (L
commutes with the diagonal S). Rot' = R * Rot * L is a proper rotation, the scale is unchanged, and the geometry is
reflected by L, which is the identity for a kind that is symmetric about that local plane. b is chosen among the
kind's exact symmetry planes (``SYMMETRY``), the local axis most aligned with the mirror axis first:

* box: x, y, z; ``inset_faces`` swap "+b" and "-b";
* extrude: x or y, the outline's b coordinate negated and its order reversed (still counter-clockwise), or z, free
  (the depth is centred);
* cylinder, sphere, capsule: y or z; cone, lathe: y only. Their ring has a vertex at angle 0
  (``kernel.compile_ir._lathe``), so the plane y = 0 is exact for any segment count, and z only for the kinds that
  are symmetric top to bottom;
* group: any axis (no geometry); its children are reflected about its own plane b, recursively;
* mesh_ref: a referenced file has no reflection without a negative scale, so mirroring one (or a subtree holding
  one) is an error.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

AXES = ("x", "y", "z")
SHORTCUT_FIELDS = ("mirror", "repeat")
MAX_REPEAT = 32
# Nested repeats multiply (a ring of 8 bolts on each of 4 repeated wheels is 32 bolts); a design of several hundred
# parts is already far past any prop's triangle budget, so an expansion past this is a mistake, reported as one.
MAX_EXPANDED_NODES = 512
ID_MAX = 64
# Local symmetry planes per kind, in preference order when two are equally aligned (see the module docstring).
SYMMETRY: dict[str, str] = {"box": "xyz", "extrude": "xyz", "cylinder": "yz", "sphere": "yz", "capsule": "yz",
                            "cone": "y", "lathe": "y", "group": "xyz", "mesh_ref": ""}
_SRC = "\0source"  # internal key: the input node a copy was made from (never a field name)
_DIGITS = 9  # rotations are recomputed through a matrix: 1e-9 degrees / metres is far below float32 in USD


class ShortcutError(ValueError):
    """A mirror or repeat that cannot be expanded; the message names the node and what to change."""


def has_shortcuts(node: dict[str, Any]) -> bool:
    return any(node.get(k) is not None for k in SHORTCUT_FIELDS)


def check(node: dict[str, Any]) -> None:
    """The rules one node's shortcut fields must meet on their own (the wire model's validator calls this)."""
    nid, kind = node.get("id"), node.get("kind")
    rep = node.get("repeat")
    if rep is not None:
        if not isinstance(rep, dict) or ("offset" in rep) == ("around" in rep) or "count" not in rep:
            raise ShortcutError(f"{nid}: repeat is {{count, offset}} (a row) or {{count, around}} (a ring), not both")
        if not 2 <= int(rep["count"]) <= MAX_REPEAT:
            raise ShortcutError(f"{nid}: repeat count must be 2 to {MAX_REPEAT} (the original included)")
    if node.get("mirror") is not None and kind == "mesh_ref":
        raise ShortcutError(f"{nid}: a mesh_ref cannot be mirrored (a referenced file has no reflection without a negative "
                            "scale); add the mirrored copy as its own node")


# --- transforms ------------------------------------------------------------------------------------------------


def _rot(deg: Any) -> np.ndarray:
    """USD rotateXYZ (X applied first): R = Rz Ry Rx, as kernel.settle and tactics.authoring compute it."""
    rx, ry, rz = np.radians(np.asarray(deg, float))
    cx, sx, cy, sy, cz, sz = np.cos(rx), np.sin(rx), np.cos(ry), np.sin(ry), np.cos(rz), np.sin(rz)
    return (np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1.0]]) @ np.array([[cy, 0, sy], [0, 1.0, 0], [-sy, 0, cy]])
            @ np.array([[1.0, 0, 0], [0, cx, -sx], [0, sx, cx]]))


def _neg(v: float) -> float:
    return -v if v else 0.0  # exact, and no -0.0


def _clean(v: float) -> float:
    r = round(float(v), _DIGITS)
    return 0.0 if r == 0 else r  # no -0.0


def _euler(m: np.ndarray) -> tuple[float, float, float]:
    """XYZ Euler degrees of a proper rotation matrix (inverse of ``_rot``)."""
    s = -m[2, 0]
    if abs(s) < 1.0 - 1e-12:
        a, b, c = math.atan2(m[2, 1], m[2, 2]), math.asin(s), math.atan2(m[1, 0], m[0, 0])
    elif s > 0:  # b = +90: only a - c is defined; c = 0
        a, b, c = math.atan2(m[0, 1], m[0, 2]), math.pi / 2, 0.0
    else:  # b = -90: only a + c is defined; c = 0
        a, b, c = math.atan2(-m[0, 1], -m[0, 2]), -math.pi / 2, 0.0
    out = []
    for x in (a, b, c):
        d = _clean(math.degrees(x))
        out.append(180.0 if d == -180.0 else d)  # one spelling of a half turn
    return out[0], out[1], out[2]


def _transform(node: dict[str, Any]) -> dict[str, Any]:
    t = dict(node.get("transform") or {})
    t.setdefault("translate", (0.0, 0.0, 0.0))
    t.setdefault("rotate_euler_deg", (0.0, 0.0, 0.0))
    t.setdefault("scale", (1.0, 1.0, 1.0))
    return t


def _same_rotation(m: np.ndarray, deg: Any) -> bool:
    return bool(np.allclose(_rot(deg), m, atol=1e-12))


def _rotated(node: dict[str, Any], turn: np.ndarray) -> dict[str, Any]:
    t = _transform(node)
    rot = turn @ _rot(t["rotate_euler_deg"])
    return {**t, "translate": tuple(_clean(x) for x in turn @ np.asarray(t["translate"], float)),
            "rotate_euler_deg": t["rotate_euler_deg"] if _same_rotation(rot, t["rotate_euler_deg"]) else _euler(rot)}


# --- mirror -----------------------------------------------------------------------------------------------------


def _reflect_params(kind: str, params: dict[str, Any], b: int) -> dict[str, Any]:
    """The node's own geometry reflected along its local axis ``b`` (identity where the kind is symmetric there)."""
    axis = AXES[b]
    if kind == "box" and params.get("inset_faces"):
        swap = {f"+{axis}": f"-{axis}", f"-{axis}": f"+{axis}"}
        return {**params, "inset_faces": [swap.get(f, f) for f in params["inset_faces"]]}
    if kind == "extrude" and b < 2 and params.get("outline"):
        flipped = [tuple(_neg(c) if i == b else c for i, c in enumerate(p)) for p in params["outline"]]
        return {**params, "outline": flipped[::-1]}  # reversed: a reflection turns counter-clockwise into clockwise
    return params


def _mirror_one(node: dict[str, Any], a: int) -> tuple[dict[str, Any], int]:
    """``node`` reflected through the plane of its parent's axis ``a``; returns it and the local axis b used."""
    kind = node["kind"]
    if kind == "mesh_ref":
        raise ShortcutError(f"{node.get(_SRC, node['id'])}: a mesh_ref cannot be mirrored (a referenced file has no reflection without a "
                            "negative scale); add the mirrored copy as its own node")
    t = _transform(node)
    rot = _rot(t["rotate_euler_deg"])
    allowed = [AXES.index(c) for c in SYMMETRY[kind]]
    b = max(allowed, key=lambda i: (round(abs(rot[a, i]), _DIGITS), -allowed.index(i)))
    r, lb = np.eye(3), np.eye(3)
    r[a, a], lb[b, b] = -1.0, -1.0
    new_rot = r @ rot @ lb
    translate = tuple(_neg(x) if i == a else x for i, x in enumerate(t["translate"]))
    euler = t["rotate_euler_deg"] if _same_rotation(new_rot, t["rotate_euler_deg"]) else _euler(new_rot)
    out = {**node, "transform": {**t, "translate": translate, "rotate_euler_deg": euler},
           "params": _reflect_params(kind, dict(node.get("params") or {}), b)}
    return out, b


def _mirror_subtree(nodes: list[dict[str, Any]], a: int) -> list[dict[str, Any]]:
    """A subtree (root first) reflected through its root's parent plane ``a``: each child about its parent's plane b."""
    by_parent: dict[str | None, list[dict[str, Any]]] = {}
    ids = {n["id"] for n in nodes}
    for n in nodes[1:]:
        by_parent.setdefault(n.get("parent"), []).append(n)
    out: dict[str, dict[str, Any]] = {}

    def walk(n: dict[str, Any], axis: int) -> None:
        m, b = _mirror_one(n, axis)
        out[n["id"]] = m
        for c in by_parent.get(n["id"], ()):
            walk(c, b)

    walk(nodes[0], a)
    assert set(out) == ids, "subtree is not connected"  # built by _expand, never from input directly
    return [out[n["id"]] for n in nodes]


# --- expansion --------------------------------------------------------------------------------------------------


def _renamed(nodes: list[dict[str, Any]], suffix: str) -> list[dict[str, Any]]:
    """A subtree copy's ids: the root's id + ``suffix``; every other node is named inside it, ``<new root id>__<id>``.
    A suffix alone would collide when a child repeats inside a repeated parent (the child's own copy c__r1 and the
    original child in the parent's copy 1); a subtree's ids are unique, so ids prefixed with a unique root are too."""
    root = nodes[0]["id"]
    new_root = root + suffix
    name = lambda i: new_root if i == root else f"{new_root}__{i}"  # noqa: E731
    ids = {n["id"] for n in nodes}
    out = []
    for n in nodes:
        new = {**n, "id": name(n["id"]), _SRC: n.get(_SRC, n["id"])}
        if n is not nodes[0] and n.get("parent") in ids:
            new["parent"] = name(n["parent"])
        out.append(new)
    return out


def _plain(node: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in node.items() if k not in SHORTCUT_FIELDS}


def _expand(node: dict[str, Any], children: dict[str, list[dict[str, Any]]], budget: list[int]) -> list[list[dict[str, Any]]]:
    """Instances of ``node``'s subtree (each a node list, root first), the original instance first."""
    check(node)
    base = [_plain(node)]
    for child in children.get(node["id"], ()):
        for inst in _expand(child, children, budget):
            base += inst
    instances = [base]
    rep = node.get("repeat") or {}
    count = rep.get("count")
    mirror = node.get("mirror")
    made = len(base) * ((count or 1) * (2 if mirror else 1) - 1)
    budget[0] += made
    if budget[0] > MAX_EXPANDED_NODES:
        raise ShortcutError(f"{node['id']}: mirror/repeat would make more than {MAX_EXPANDED_NODES} nodes; repeat fewer "
                            "instances or fewer parts under it")
    if count:
        if rep.get("offset") is not None:
            step = np.asarray(rep["offset"], float)
            for k in range(1, count):
                t = _transform(base[0])
                moved = tuple(_clean(x) for x in np.asarray(t["translate"], float) + k * step)
                inst = _renamed(base, f"__r{k}")
                inst[0] = {**inst[0], "transform": {**t, "translate": moved}}
                instances.append(inst)
        else:
            axis = AXES.index(rep["around"])
            for k in range(1, count):
                deg = [0.0, 0.0, 0.0]
                deg[axis] = 360.0 * k / count
                inst = _renamed(base, f"__r{k}")
                inst[0] = {**inst[0], "transform": _rotated(base[0], _rot(deg))}
                instances.append(inst)
    if mirror:
        a = AXES.index(mirror)
        for inst in instances:
            _one_side(inst, a, node["id"])
        instances += [_mirror_subtree(_renamed(inst, f"__m{mirror}"), a) for inst in list(instances)]
    return instances


STRADDLE_TOL = 1e-4  # metres: a part that only touches the plane (two halves meeting) is on one side


def local_bounds(kind: str, p: dict[str, Any]) -> tuple[np.ndarray, np.ndarray] | None:
    """(lo, hi) of a node's geometry in its own frame from its params; None for a group or a mesh_ref. A lathe is
    its widest radius all round and its profile's z range; an extrusion its outline's box and the centred depth."""
    h: np.ndarray | None = None
    if kind == "box":
        h = np.asarray(p["size"], float) / 2
    elif kind in ("cylinder", "cone"):
        h = np.array([p["radius"], p["radius"], float(p["height"]) / 2])
    elif kind == "sphere":
        h = np.full(3, float(p["radius"]))
    elif kind == "capsule":
        h = np.array([p["radius"], p["radius"], float(p["height"]) / 2 + float(p["radius"])])
    elif kind == "lathe":
        r = max(float(q[0]) for q in p["profile"])
        z = [float(q[1]) for q in p["profile"]]
        return np.array([-r, -r, min(z)]), np.array([r, r, max(z)])
    elif kind == "extrude":
        xy = np.asarray(p["outline"], float)
        d = float(p["depth"]) / 2
        return np.array([*xy.min(0), -d]), np.array([*xy.max(0), d])
    return None if h is None else (-h, h)


def _matrix(node: dict[str, Any]) -> np.ndarray:
    t = _transform(node)
    m = np.eye(4)
    m[:3, :3] = _rot(t["rotate_euler_deg"]) @ np.diag(np.asarray(t["scale"], float))
    m[:3, 3] = t["translate"]
    return m


def _local_points(n: dict[str, Any]) -> np.ndarray | None:
    """Points whose extent is the node's geometry in its own frame: the kernel's own mesh of a primitive
    (``compile_ir.primitive_mesh``, a few hundred points at most), else the corners of ``local_bounds``; None for a
    group, a mesh_ref, or params the IR validation will refuse. The mesh, not its box: the corners of a rotated
    cylinder's box stand up to (sqrt(2) - 1) * radius outside it, and a part wholly on one side was refused for
    them (review 2026-11-22, r5: a cylinder 2 mm clear of x = 0 reported as spanning x -0.015 to 0.205)."""
    from antigravity3d import ir
    from antigravity3d.kernel import compile_ir

    kind, params = n["kind"], n.get("params") or {}
    if kind in compile_ir.PRIMITIVES:
        try:
            pts, _ = compile_ir.primitive_mesh(ir.NodeIR.model_validate({"id": n["id"], "kind": kind, "params": params}))
            return np.asarray(pts, np.float64)
        except (KeyError, TypeError, ValueError):
            return None  # malformed params: the IR validation names them
    try:
        box = local_bounds(kind, params)
    except (KeyError, TypeError, ValueError):
        return None
    if box is None:
        return None
    return np.array([[x, y, z] for x in (box[0][0], box[1][0]) for y in (box[0][1], box[1][1]) for z in (box[0][2], box[1][2])])


def _one_side(inst: list[dict[str, Any]], a: int, nid: str) -> None:
    """A mirrored part must lie on one side of the plane: one that straddles it would be copied onto itself. Live
    (2026-11-22, worker tier) one crate in five mirrored a hinge pin that sat on its group's plane, making a coincident
    duplicate (z-fighting, its triangles twice). Checked on the corners of every geometric node of the instance, in
    the frame the mirror acts in (the root's parent)."""
    by = {n["id"]: n for n in inst}
    cache: dict[str, np.ndarray] = {}

    def frame(n: dict[str, Any]) -> np.ndarray:
        if n["id"] not in cache:
            up = by.get(n.get("parent")) if n is not inst[0] else None
            cache[n["id"]] = (frame(up) if up is not None else np.eye(4)) @ _matrix(n)
        return cache[n["id"]]

    lo, hi = math.inf, -math.inf
    for n in inst:
        pts = _local_points(n)
        if pts is None:
            continue
        w = (frame(n) @ np.c_[pts, np.ones(len(pts))].T)[a]
        lo, hi = min(lo, float(w.min())), max(hi, float(w.max()))
    if lo < -STRADDLE_TOL and hi > STRADDLE_TOL:
        where = "its parent's" if inst[0].get("parent") else "the asset's"
        raise ShortcutError(f"{nid}: mirror {AXES[a]} needs the part on one side of {where} {AXES[a]} = 0 plane, but it "
                            f"spans {AXES[a]} {lo:.3f} to {hi:.3f}, so the copy would overlap it; move it to one side, "
                            "or drop mirror if it is already symmetric")


def expand(nodes: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    """(the input nodes without shortcut keys, in order; [(source id, copy)] in the order made).

    The source id is the input node a copy was made from. A node whose parent is not in ``nodes`` (a patch that adds
    a child to an existing part) is a root here: its copies are made in that parent's frame all the same."""
    plain = [_plain(n) for n in nodes]
    if not any(has_shortcuts(n) for n in nodes):
        return plain, []
    ids = [n["id"] for n in nodes]
    known = set(ids)
    children: dict[str, list[dict[str, Any]]] = {}
    roots = []
    for n in nodes:
        if n.get("parent") in known and n.get("parent") != n["id"]:
            children.setdefault(n["parent"], []).append(n)
        else:
            roots.append(n)
    budget = [len(nodes)]
    made: list[dict[str, Any]] = []
    for root in roots:
        for inst in _expand(root, children, budget):
            made += inst
    copies, seen = [], set(known)
    for n in made:
        if _SRC not in n:
            continue  # an original
        if n["id"] in seen:
            raise ShortcutError(f"{n['id']}: the id made for a mirror/repeat copy already exists; rename the original")
        if len(n["id"]) > ID_MAX:
            raise ShortcutError(f"{n['id']}: the id made for a mirror/repeat copy is longer than {ID_MAX} characters; "
                                "shorten the original's id")
        seen.add(n["id"])
        copies.append((n[_SRC], {k: v for k, v in n.items() if k != _SRC}))
    return plain, copies
