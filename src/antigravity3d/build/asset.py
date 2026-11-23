"""``Asset``: the object a build script writes one asset with (``from agy3d import Asset``).

The reference the script's author reads is ``plugin_script_mode/SKILL.md``; every call, keyword and default there is
built here exactly. What each call becomes:

* a ready shape (box, cylinder, cone, sphere, capsule, lathe, extrude) is a node of the scene format in its wire form
  (``ir_schema``), validated when the call is made by the wire model and ``kernel.compile_ir.check_node`` (the
  kernel's own messages), measured with the kernel's own mesh (``primitive_mesh`` under the node's transform) and
  costed with ``triangle_count``. Nothing about a primitive is computed here a second time.
* a free-form part (tube, loft, mesh, blender, load) is a ``mesh_ref`` node whose PLY file ``save()`` writes; its
  placement, orientation and mirror copy are baked into the vertices (the node's transform is the identity).

A wrong call raises ``BuildError`` at that line of the script, naming the part. Nothing is clamped. The same script
writes the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError

from antigravity3d import ir, ir_schema, ir_shortcuts
from antigravity3d.build import shapes
from antigravity3d.build.shapes import BuildError
from antigravity3d.kernel import compile_ir

OUT_ENV = "AGY3D_BUILD_OUT"
ENGINES = ("unreal_5", "unity_urp", "godot_4", "roblox_r15", "web_three")  # state.TargetEngine (tests/test_build_asset.py pins it)
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ID_MAX = ir_shortcuts.ID_MAX
SHOWN_DIGITS = 6  # bounds read back to 1 um: the kernel's meshes are float32 (0.19 is 0.18999999 there)
AXES = ir_shortcuts.AXES


def _r(v: float) -> float:
    x = round(float(v), SHOWN_DIGITS)
    return 0.0 if x == 0 else x


def _py(v: Any) -> Any:
    """Numbers as plain Python floats/ints, sequences as lists (numpy values from a script's arithmetic included)."""
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return float(v)
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_py(x) for x in v]
    return v


def _num0(v: float) -> float:
    return 0.0 if v == 0 else float(v)  # no -0.0 in the design


class Part:
    """A part as built: where it is (world bounding box, metres) and, for a tube, its centre line."""

    def __init__(self, pid: str, kind: str, lo: np.ndarray, hi: np.ndarray, centre: np.ndarray | None = None):
        self.id = pid
        self.kind = kind
        self._lo, self._hi = np.asarray(lo, np.float64), np.asarray(hi, np.float64)
        self._centre = centre
        self.mirrored: Part | None = None  # the reflected copy (``mirror=``), None when there is none

    @property
    def min(self) -> tuple[float, float, float]:
        return tuple(_r(x) for x in self._lo)  # type: ignore[return-value]

    @property
    def max(self) -> tuple[float, float, float]:
        return tuple(_r(x) for x in self._hi)  # type: ignore[return-value]

    @property
    def center(self) -> tuple[float, float, float]:
        return tuple(_r(x) for x in (self._lo + self._hi) / 2)  # type: ignore[return-value]

    @property
    def size(self) -> tuple[float, float, float]:
        return tuple(_r(x) for x in self._hi - self._lo)  # type: ignore[return-value]

    @property
    def top(self) -> float:
        return _r(self._hi[2])

    @property
    def bottom(self) -> float:
        return _r(self._lo[2])

    def along(self, t: float) -> tuple[float, float, float]:
        """The point at fraction ``t`` (0 to 1) of a tube's centre line as built (smoothed, placed, mirrored)."""
        if self._centre is None:
            raise BuildError(f"{self.kind} {self.id}: along() is for tube parts; use min, max, center, top, bottom")
        if isinstance(t, bool) or not isinstance(t, (int, float, np.floating, np.integer)) or not 0.0 <= t <= 1.0:
            raise BuildError(f"tube {self.id}: along(t) takes t from 0 (the first point) to 1 (the last), got {t!r}")
        return tuple(_r(x) for x in shapes.along(self._centre, float(t)))  # type: ignore[return-value]

    def __repr__(self) -> str:
        return f"Part({self.kind} {self.id}, min={self.min}, max={self.max})"


class Asset:
    """One asset: its materials and parts, written by ``save()`` as the hand-off folder (``build.handoff``)."""

    def __init__(self, name: str, engine: str, max_tris: int = 15000, brief: str = "", art_direction: str = ""):
        if not isinstance(name, str) or not ID_RE.match(name) or len(name) > ID_MAX:
            raise BuildError(f"Asset: name must be letters, digits and underscores, not starting with a digit, at most "
                             f"{ID_MAX} characters (it names the files), got {name!r}")
        if engine not in ENGINES:
            raise BuildError(f"Asset {name}: engine must be one of {', '.join(ENGINES)}, got {engine!r}")
        if isinstance(max_tris, bool) or not isinstance(max_tris, (int, np.integer)) or max_tris <= 0:
            raise BuildError(f"Asset {name}: max_tris must be a positive whole number, got {max_tris!r}")
        if not isinstance(brief, str):
            raise BuildError(f"Asset {name}: brief must be text (the user's request in their words)")
        if not isinstance(art_direction, str):
            raise BuildError(f"Asset {name}: art_direction must be a style id (text), got {art_direction!r}")
        if art_direction:
            from antigravity3d import (
                art_direction as styles,  # its own check; yaml is imported only when one is named
            )

            try:
                styles.load(art_direction)
            except styles.UnknownArtDirection as e:
                raise BuildError(f"Asset {name}: {e}") from None
        self.name, self.engine, self.max_tris, self.brief = name, engine, int(max_tris), brief
        self.art_direction = art_direction
        self._materials: list[dict[str, Any]] = []
        self._nodes: list[dict[str, Any]] = []  # wire nodes, in the order made (a mirror copy right after its part)
        self._files: dict[str, bytes] = {}  # node id -> PLY bytes of a free-form part
        self._tris: dict[str, int] = {}
        self._parts: dict[str, Part] = {}
        self._notes: list[str] = []

    # --- materials ------------------------------------------------------------------------------------------------

    def material(self, id: str, color: str, surface: str = "", metallic: float = 0.0, roughness: float = 0.5) -> str:
        """A material; returns its id, which a shape's ``material=`` takes. ``color`` is "#rrggbb" as a colour picker
        shows it (the scene stores it as written; the colour rule converts it once, ``ir_schema.linear_colour``)."""
        self._check_id(id, "material", materials=True)
        wire = {"id": id, "base_color": _py(color), "metallic": _py(metallic), "roughness": _py(roughness)}
        if surface:
            wire["surface"] = surface
        where = f"material {id}"
        if not (isinstance(color, str) and re.fullmatch(ir_schema.HEX_COLOUR, color)):
            raise BuildError(f"{where}: color must be \"#rrggbb\" as a colour picker shows it, got {color!r}")
        try:
            m = ir_schema.WireMaterial.model_validate(wire)
            ir.MaterialIR.model_validate(ir_schema.ir_linear(m.model_dump(exclude_unset=True)))
        except ValidationError as e:
            raise BuildError("; ".join(ir_schema._explain(where, e)[:3])) from None
        except ValueError as e:
            raise BuildError(f"{where}: {e}") from None
        self._materials.append(m.model_dump(mode="json", exclude_unset=True))
        return id

    # --- ready shapes ---------------------------------------------------------------------------------------------

    def box(self, id: str, size, bevel: float = 0, bevel_segments: int = 1, inset_faces=(), inset_margin: float = 0,
            inset_depth: float = 0, *, material: str | None = None, at=None, bottom=None, top=None, axis=None, rot=None,
            mirror: str | None = None) -> Part:
        return self._ready("box", id, {"size": size, "bevel": bevel, "bevel_segments": bevel_segments,
                                       "inset_faces": inset_faces, "inset_margin": inset_margin, "inset_depth": inset_depth},
                           material, at, bottom, top, axis, rot, mirror)

    def cylinder(self, id: str, radius: float, height: float, segments: int = 24, bevel: float = 0,
                 bevel_segments: int = 1, *,
                 material: str | None = None, at=None, bottom=None, top=None, axis=None, rot=None,
                 mirror: str | None = None) -> Part:
        return self._ready("cylinder", id, {"radius": radius, "height": height, "segments": segments, "bevel": bevel,
                                            "bevel_segments": bevel_segments}, material, at, bottom, top, axis, rot, mirror)

    def cone(self, id: str, radius: float, height: float, segments: int = 24, *, material: str | None = None, at=None,
             bottom=None, top=None, axis=None, rot=None, mirror: str | None = None) -> Part:
        return self._ready("cone", id, {"radius": radius, "height": height, "segments": segments},
                           material, at, bottom, top, axis, rot, mirror)

    def sphere(self, id: str, radius: float, segments: int = 24, rings: int = 12, *, material: str | None = None,
               at=None, bottom=None, top=None, axis=None, rot=None, mirror: str | None = None) -> Part:
        return self._ready("sphere", id, {"radius": radius, "segments": segments, "rings": rings},
                           material, at, bottom, top, axis, rot, mirror)

    def capsule(self, id: str, radius: float, height: float, segments: int = 16, *, material: str | None = None,
                at=None, bottom=None, top=None, axis=None, rot=None, mirror: str | None = None) -> Part:
        return self._ready("capsule", id, {"radius": radius, "height": height, "segments": segments},
                           material, at, bottom, top, axis, rot, mirror)

    def lathe(self, id: str, profile, segments: int = 24, closed: bool = False, *, material: str | None = None, at=None,
              bottom=None, top=None, axis=None, rot=None, mirror: str | None = None) -> Part:
        return self._ready("lathe", id, {"profile": profile, "segments": segments, "closed": closed},
                           material, at, bottom, top, axis, rot, mirror)

    def extrude(self, id: str, outline, depth: float, *, material: str | None = None, at=None, bottom=None, top=None,
                axis=None, rot=None, mirror: str | None = None) -> Part:
        return self._ready("extrude", id, {"outline": outline, "depth": depth},
                           material, at, bottom, top, axis, rot, mirror)

    # --- free-form parts ------------------------------------------------------------------------------------------

    def tube(self, id: str, path, radius, sides: int = 10, closed: bool = False, smooth: bool = False, *,
             material: str | None = None, at=None, bottom=None, top=None, axis=None, rot=None,
             mirror: str | None = None) -> Part:
        """A rod along ``path`` (see ``shapes.tube``: mitred corners, no twist; smooth=True adds 8 rings per span)."""
        self._check_id(id, "tube")
        for flag, v in (("closed", closed), ("smooth", smooth)):
            if not isinstance(v, bool):
                raise BuildError(f"tube {id}: {flag} must be True or False, got {v!r}")
        t = shapes.tube(id, _py(path), _py(radius), _py(sides), closed, smooth)
        return self._free("tube", id, t.points, t.tris, material, at, bottom, top, axis, rot, mirror, centre=t.centre)

    def loft(self, id: str, sections, *, material: str | None = None, at=None, bottom=None, top=None, axis=None,
             rot=None, mirror: str | None = None) -> Part:
        """A skin through ``sections`` (see ``shapes.loft``)."""
        self._check_id(id, "loft")
        pts, tris, flipped = shapes.loft(id, _py(sections))
        notes = [f"loft {id}: faces flipped outward (the sections run clockwise seen along the skin)"] if flipped else []
        return self._free("loft", id, pts, tris, material, at, bottom, top, axis, rot, mirror, notes=notes)

    def mesh(self, id: str, vertices, faces=None, *, material: str | None = None, at=None, bottom=None, top=None,
             axis=None, rot=None, mirror: str | None = None) -> Part:
        """Any mesh: ``vertices`` (N x 3) and ``faces`` (lists of vertex indices: triangles, or polygons of any size,
        which are split as fans from their first corner, so a polygon must be convex); or a ``trimesh.Trimesh`` as
        ``vertices`` alone. Coincident points are welded, degenerate and duplicate faces dropped and the winding made
        consistent and outward with the project's sanitizer (``kernel.mesh_repair``); each repair is one note. An open
        mesh is kept (the session's check reports it)."""
        self._check_id(id, "mesh")
        where = f"mesh {id}"
        if faces is None and hasattr(vertices, "vertices") and hasattr(vertices, "faces"):
            vertices, faces = vertices.vertices, vertices.faces
        if faces is None:
            raise BuildError(f"{where}: faces is missing; give vertices and faces, or a trimesh.Trimesh")
        try:
            v = np.asarray(vertices, np.float64)
        except (TypeError, ValueError):
            raise BuildError(f"{where}: vertices must be a list of (x, y, z) points") from None
        if v.ndim != 2 or v.shape[1] != 3 or not len(v) or not np.isfinite(v).all():
            raise BuildError(f"{where}: vertices must be a non-empty list of finite (x, y, z) points")
        tris = []
        for i, f in enumerate(_py(list(faces) if not isinstance(faces, np.ndarray) else faces.tolist())):
            if isinstance(f, list) and all(isinstance(k, float) and k.is_integer() for k in f if not isinstance(k, int)):
                f = [int(k) if isinstance(k, (int, float)) and not isinstance(k, bool) else k for k in f]  # 3.0 is 3
            if not isinstance(f, list) or len(f) < 3 or not all(isinstance(k, int) and not isinstance(k, bool) for k in f):
                raise BuildError(f"{where}: face {i} must be a list of at least 3 vertex indices (whole numbers), got {f!r}")
            if min(f) < 0 or max(f) >= len(v):
                raise BuildError(f"{where}: face {i} uses vertex {max(f) if max(f) >= len(v) else min(f)}, but there "
                                 f"are {len(v)} vertices (0 to {len(v) - 1})")
            tris += [(f[0], f[k], f[k + 1]) for k in range(1, len(f) - 1)]
        if not tris:
            raise BuildError(f"{where}: faces is empty; a mesh needs at least one face")
        from antigravity3d.kernel.mesh_repair import repair_mesh
        from antigravity3d.kernel.types import MeshData

        fixed, c = repair_mesh(MeshData(id, "", v, np.asarray(tris, np.int32)))
        if not len(fixed.triangles):
            raise BuildError(f"{where}: every face is degenerate (no area); nothing is left to build")
        notes = []
        for key, text in (("merged_vertices", "duplicate points merged"), ("degenerate_faces", "degenerate faces dropped"),
                          ("duplicate_faces", "duplicate faces dropped"),
                          ("reoriented_faces", "faces flipped to a consistent outward winding")):
            if c.get(key):
                notes.append(f"mesh {id}: {c[key]} {text}")
        return self._free("mesh", id, fixed.points, fixed.triangles, material, at, bottom, top, axis, rot, mirror,
                          notes=notes)

    def blender(self, id: str, code: str, *, material: str | None = None, at=None, bottom=None, top=None, axis=None,
                rot=None, mirror: str | None = None) -> Part:
        """Every mesh-like object a Blender Python snippet leaves in an empty scene, modifiers applied, joined
        (``build.blender_part``: linted, run headless, cached)."""
        self._check_id(id, "blender")
        from antigravity3d.build import blender_part

        pts, tris, objects, skipped, _cached = blender_part.run(id, code)
        left = "; ".join(f"{n} {why} object{'s' if n != 1 else ''} left out" for why, n in skipped.items())
        if not len(tris):
            raise BuildError(f"blender {id}: the snippet left no visible mesh, curve, surface or text object in the scene"
                             + (f" ({left})" if left else ""))
        note = (f"blender {id}: {objects} object{'s' if objects != 1 else ''} joined, {len(tris):,} triangles"
                + (f"; {left}" if left else ""))
        return self._free("blender", id, pts, tris, material, at, bottom, top, axis, rot, mirror, notes=[note])

    def load(self, id: str, path: str, *, material: str | None = None, at=None, bottom=None, top=None, axis=None,
             rot=None, mirror: str | None = None) -> Part:
        """A mesh file (glb, gltf, obj, ply, stl) read as the kernel reads one (``kernel.mesh_io.triangle_parts``),
        relative to the script's folder, and written again as this part's own file."""
        self._check_id(id, "load")
        from antigravity3d.kernel.mesh_io import triangle_parts

        where = f"load {id}"
        if not isinstance(path, (str, os.PathLike)) or not str(path):
            raise BuildError(f"{where}: path must be the mesh file's path, relative to the script's folder")
        p = Path(path)
        pts, tris, off = [], [], 0
        try:
            for _, v, f, _uv in triangle_parts(p):
                pts.append(np.asarray(v, np.float64))
                tris.append(np.asarray(f, np.int32) + off)
                off += len(v)
        except FileNotFoundError:
            raise BuildError(f"{where}: no file {str(path)!r} (paths are relative to the script's folder)") from None
        except ValueError as e:
            raise BuildError(f"{where}: {str(e).replace(str(p), repr(str(path)))}; glb, gltf, obj, ply or stl") from None
        return self._free("load", id, np.concatenate(pts), np.concatenate(tris), material, at, bottom, top, axis, rot,
                          mirror)

    # --- save -----------------------------------------------------------------------------------------------------

    def save(self) -> Path:
        """Writes the hand-off folder (``$AGY3D_BUILD_OUT``, else ``./agy3d_build/<name>/``), replacing what an
        earlier save left there (its design.json and parts/ only), prints one line and returns the folder."""
        if not self._nodes:
            raise BuildError(f"Asset {self.name}: no parts to save; add shapes before a.save()")
        out = Path(os.environ.get(OUT_ENV) or Path("agy3d_build") / self.name)
        doc = self.design()
        try:
            ir_schema.parse_tool_scene(self.name, doc["nodes"], doc["materials"])  # what the session will parse
        except ValueError as e:
            raise BuildError(f"Asset {self.name}: the design does not read as a scene: {e}") from None
        out.mkdir(parents=True, exist_ok=True)
        if (out / "parts").exists():
            shutil.rmtree(out / "parts")
        (out / "design.json").unlink(missing_ok=True)
        if self._files:
            (out / "parts").mkdir()
        for node in self._nodes:
            if node["id"] in self._files:
                (out / node["params"]["path"]).write_bytes(self._files[node["id"]])
        (out / "design.json").write_text(json.dumps(doc, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        s = doc["stats"]
        print(f"agy3d: wrote {s['parts']} parts, {len(self._materials)} materials, {s['triangles']:,} triangles to {out}")
        return out

    def design(self) -> dict[str, Any]:
        """The document ``save()`` writes as design.json (CONTRACT section 2)."""
        lo = np.min([p._lo for p in self._all_parts()], axis=0)
        hi = np.max([p._hi for p in self._all_parts()], axis=0)
        return {
            "version": 1,
            "asset": {"name": self.name, "engine": self.engine, "max_tris": self.max_tris, "brief": self.brief,
                      "art_direction": self.art_direction},
            "materials": [dict(m) for m in self._materials],
            "nodes": [json.loads(json.dumps(n)) for n in self._nodes],
            "notes": list(self._notes),
            "stats": {"parts": len(self._nodes), "triangles": sum(self._tris.values()),
                      "bounds": [[_r(x) for x in lo], [_r(x) for x in hi]]},
        }

    # --- internals ------------------------------------------------------------------------------------------------

    def _all_parts(self) -> list[Part]:
        return [self._parts[n["id"]] for n in self._nodes]

    def _check_id(self, pid: Any, kind: str, *, materials: bool = False) -> None:
        if not isinstance(pid, str) or not ID_RE.match(pid) or len(pid) > ID_MAX:
            raise BuildError(f"{kind} {pid!r}: an id is letters, digits and underscores, not starting with a digit, at "
                             f"most {ID_MAX} characters")
        taken = {m["id"] for m in self._materials} if materials else set(self._parts)
        if pid in taken:
            raise BuildError(f"{kind} {pid}: the id {pid!r} is already used by another "
                             f"{'material' if materials else 'part'}; ids are unique")

    def _material(self, kind: str, pid: str, material: Any) -> str | None:
        if material is None:
            return None
        known = [m["id"] for m in self._materials]
        if material not in known:
            raise BuildError(f"{kind} {pid}: material {material!r} is not defined; a.material(...) first "
                             f"(defined: {', '.join(known) or 'none'})")
        return material

    def _ready(self, kind: str, pid: str, params: dict[str, Any], material, at, bottom, top, axis, rot, mirror) -> Part:
        self._check_id(pid, kind)
        where = f"{kind} {pid}"
        mat = self._material(kind, pid, material)
        defaults = ir.NODE_PARAMS[kind].model_fields
        written = {}
        for k, v in params.items():
            v = _py(v)
            d = defaults[k].get_default(call_default_factory=True) if k in defaults else None
            if k in defaults and not defaults[k].is_required() and v == _py(d):
                continue  # the kernel's own default: left out, as a written answer leaves it out
            written[k] = v
        euler, rmat = _orientation(where, axis, rot, upright=kind == "extrude")
        mirror = _mirror_axis(where, mirror)
        node: dict[str, Any] = {"id": pid, "kind": kind, "params": written, "transform": {"translate": [0.0, 0.0, 0.0]}}
        if euler != (0.0, 0.0, 0.0):
            node["transform"]["rotate_euler_deg"] = list(euler)
        if mat is not None:
            node["material_id"] = mat
        local = self._kernel_points(where, node)
        world = local @ rmat.T
        node["transform"]["translate"] = [_num0(x) for x in _place(where, world, at, bottom, top)]
        lo, hi = world.min(0) + node["transform"]["translate"], world.max(0) + node["transform"]["translate"]
        tris = compile_ir.triangle_count(ir.NodeIR.model_validate(node))
        part = Part(pid, kind, lo, hi)
        self._add(node, part, tris)
        if mirror:
            try:
                _, copies = ir_shortcuts.expand([{**node, "mirror": mirror}])
            except ir_shortcuts.ShortcutError as e:
                self._drop(pid)
                raise BuildError(f"{kind} {e}") from None
            copy = copies[0][1]
            if copy["id"] in self._parts:
                self._drop(pid)
                raise BuildError(f"{where}: the mirror copy's id {copy['id']!r} is already used by another part")
            cn = ir.NodeIR.model_validate(copy)
            pts = self._kernel_points(where, copy) @ ir_shortcuts._rot(cn.transform.rotate_euler_deg).T + cn.transform.translate
            part.mirrored = Part(copy["id"], kind, pts.min(0), pts.max(0))
            self._add(json.loads(json.dumps(copy)), part.mirrored, tris)
        return part

    @staticmethod
    def _kernel_points(where: str, node: dict[str, Any]) -> np.ndarray:
        """The node's mesh in its own frame, from the kernel, after the wire model and the kernel's checks pass."""
        try:
            wire = ir_schema.NODE_BY_KIND[node["kind"]].model_validate(node)
            n = ir.NodeIR.model_validate(ir_schema._node_dump(wire))
            compile_ir.check_node(n)
            pts, _ = compile_ir.primitive_mesh(n)
        except ValidationError as e:
            raise BuildError(_param_errors(where, node["params"], e)) from None
        except ValueError as e:
            msg = str(e)
            raise BuildError(f"{node['kind']} {msg}" if msg.startswith(f"{node['id']}:") else f"{where}: {msg}") from None
        return np.asarray(pts, np.float64)

    def _free(self, kind: str, pid: str, pts: np.ndarray, tris: np.ndarray, material, at, bottom, top, axis, rot, mirror,
              *, centre: np.ndarray | None = None, notes: list[str] | None = None) -> Part:
        where = f"{kind} {pid}"
        mat = self._material(kind, pid, material)
        _, rmat = _orientation(where, axis, rot)
        mirror = _mirror_axis(where, mirror)
        world = np.asarray(pts, np.float64) @ rmat.T
        off = _place(where, world, at, bottom, top)  # at= is an offset added to the part's own coordinates
        world = world + off
        line = None if centre is None else centre @ rmat.T + off
        tris = np.asarray(tris, np.int32)
        if mirror:
            a = AXES.index(mirror)
            cid = f"{pid}__m{mirror}"
            if len(cid) > ID_MAX:  # the kernel's words for a ready shape's copy (ir_shortcuts.expand)
                raise BuildError(f"{kind} {cid}: the id made for a mirror/repeat copy is longer than {ID_MAX} characters; "
                                 "shorten the original's id")
            lo, hi = world.min(0), world.max(0)
            box = {"id": pid, "kind": "box", "params": {"size": list(np.maximum(hi - lo, 1e-9))},
                   "transform": {"translate": list((lo + hi) / 2)}}
            try:  # the kernel's own one-side rule and words, on the part's bounding box
                ir_shortcuts._one_side([box], a, pid)
            except ir_shortcuts.ShortcutError as e:
                raise BuildError(f"{kind} {e}") from None
        part = self._mesh_part(kind, pid, world, tris, mat, line)
        self._notes += notes or []
        if mirror:
            flip = np.ones(3)
            flip[AXES.index(mirror)] = -1.0
            cid = f"{pid}__m{mirror}"
            if cid in self._parts:
                self._drop(pid)
                raise BuildError(f"{where}: the mirror copy's id {cid!r} is already used by another part")
            part.mirrored = self._mesh_part(kind, cid, world * flip, tris[:, ::-1], mat,
                                            None if line is None else line * flip)
        return part

    def _mesh_part(self, kind: str, pid: str, world: np.ndarray, tris: np.ndarray, mat: str | None,
                   line: np.ndarray | None) -> Part:
        data = shapes.ply_bytes(world, tris)
        exact = world.astype(np.float32).astype(np.float64)  # what the file holds
        name = f"parts/{pid}-{hashlib.sha256(data).hexdigest()[:12]}.ply"
        node: dict[str, Any] = {"id": pid, "kind": "mesh_ref", "params": {"path": name},
                                "transform": {"translate": [0.0, 0.0, 0.0]}}
        if mat is not None:
            node["material_id"] = mat
        part = Part(pid, kind, exact.min(0), exact.max(0), line)
        self._files[pid] = data
        self._add(node, part, len(tris))
        return part

    def _add(self, node: dict[str, Any], part: Part, tris: int) -> None:
        self._nodes.append(node)
        self._parts[node["id"]] = part
        self._tris[node["id"]] = int(tris)

    def _drop(self, pid: str) -> None:
        self._nodes = [n for n in self._nodes if n["id"] != pid]
        self._parts.pop(pid, None)
        self._tris.pop(pid, None)
        self._files.pop(pid, None)


# --- wording ----------------------------------------------------------------------------------------------------


POINT_NAMES = {"size": "three numbers (x, y, z)", "profile": "two numbers (r, z)", "outline": "two numbers (x, y)"}
_SHAPE_ERRORS = frozenset({"missing", "too_short", "too_long", "tuple_type", "list_type"})


def _show(v: Any) -> str:
    """A value as the script wrote it: a point as a tuple, a list of points as a list of tuples."""
    if isinstance(v, list) and v and all(isinstance(x, list) for x in v):
        return "[" + ", ".join(_show(x) for x in v) + "]"
    if isinstance(v, list):
        return "(" + ", ".join(_show(x) for x in v) + ")"
    return repr(v)


def _param_errors(where: str, params: dict[str, Any], err: ValidationError) -> str:
    """The wire model's errors (the kernel's own checks, unchanged) worded for the script's author: the keyword as
    the script writes it, not the scene format's ``params.<name>[i]`` path. Errors elsewhere keep the kernel's words."""
    out: list[str] = []
    for e in err.errors(include_url=False):
        loc = e["loc"]
        if len(loc) < 2 or loc[0] != "params" or not isinstance(loc[1], str):
            line = ir_schema._explain(where, ValidationError.from_exception_data(err.title, [e]))[0]
        else:
            name, value = loc[1], params.get(loc[1])
            whole = len(loc) == 2 or (name == "size" and e["type"] in _SHAPE_ERRORS)
            if name == "size" and whole and not (isinstance(value, list) and len(value) == 3):
                line = f"{where}: size needs {POINT_NAMES['size']}, got {_show(value)}"
            elif name in ("profile", "outline") and len(loc) >= 3 and isinstance(loc[2], int) and (
                    len(loc) == 3 or e["type"] in _SHAPE_ERRORS):
                point = value[loc[2]] if isinstance(value, list) and loc[2] < len(value) else None
                line = f"{where}: {name} point {loc[2]} needs {POINT_NAMES[name]}, got {_show(point)}"
            elif name in ("profile", "outline") and len(loc) == 2 and e["type"] in ("too_short", "too_long"):
                most = e["type"] == "too_long"
                bound = e.get("ctx", {}).get("max_length" if most else "min_length")
                n = len(value) if isinstance(value, list) else 0
                line = f"{where}: {name} needs {'at most' if most else 'at least'} {bound} points, got {n}"
            else:
                if name == "size" and len(loc) == 3 and isinstance(loc[2], int):
                    label = f"size {AXES[loc[2]]}"
                else:
                    label = name + ("" if whole else " " + " ".join(f"item {p}" for p in loc[2:]))
                msg = e["msg"]
                if msg.startswith("Input should be "):
                    msg = "must be " + msg[len("Input should be "):]
                elif e["type"] == "missing":
                    msg = "is missing"
                else:
                    msg = msg[0].lower() + msg[1:]
                got = _show(value if whole else e.get("input"))
                line = f"{where}: {label} {msg}, got {got}"
        if line not in out:
            out.append(line)
    return "; ".join(out[:3])


# --- placement --------------------------------------------------------------------------------------------------


def _vec(where: str, name: str, v: Any) -> tuple[float, float, float]:
    v = _py(v)
    if not isinstance(v, list) or len(v) != 3 or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                                                         and math.isfinite(x) for x in v):
        raise BuildError(f"{where}: {name} must be three numbers (x, y, z), got {v!r}")
    return float(v[0]), float(v[1]), float(v[2])


def _place(where: str, world: np.ndarray, at, bottom, top) -> np.ndarray:
    """The translation that puts points ``world`` (already oriented, about their own origin) where the author said:
    ``at`` their origin (a ready shape's centre), ``bottom`` / ``top`` the middle of their bounding box's lowest /
    highest face. Exactly one, none meaning at=(0, 0, 0)."""
    given = [n for n, v in (("at", at), ("bottom", bottom), ("top", top)) if v is not None]
    if len(given) > 1:
        raise BuildError(f"{where}: give one position, at=, bottom= or top=, not {' and '.join(given)}")
    if at is not None:
        return np.asarray(_vec(where, "at", at))
    if bottom is None and top is None:
        return np.zeros(3)
    lo, hi = world.min(0), world.max(0)
    mid = (lo + hi) / 2
    if bottom is not None:
        return np.asarray(_vec(where, "bottom", bottom)) - np.array([mid[0], mid[1], lo[2]])
    return np.asarray(_vec(where, "top", top)) - np.array([mid[0], mid[1], hi[2]])


AXIS_EULER = {"x": (0.0, 90.0, 0.0), "y": (-90.0, 0.0, 0.0), "z": (0.0, 0.0, 0.0)}  # shortest turn of +Z onto the axis


def _orientation(where: str, axis, rot, *, upright: bool = False) -> tuple[tuple[float, float, float], np.ndarray]:
    """(rotate_euler_deg, its matrix) for ``axis`` (where the shape's own +Z points, by the shortest turn) or ``rot``
    (degrees, the kernel's USD rotateXYZ). The matrix is always the kernel's own of the Euler angles written.

    ``upright`` (extrude): an outline is drawn upright, so instead of the shortest turn its y stays as close to world
    up (+z) as the axis allows, and its x is world +x (axis "y"), world +y (axis "x"), in general the horizontal
    direction whose x is positive (else whose y is). The depth's sign is free (an extrusion is symmetric along it), so
    the axis may come out reversed to get that. A vertical axis turns by the shortest turn, as for every shape. With
    the shortest turn an arch drawn upright came out upside down for axis "y" (review 2026-11-22, r6)."""
    if axis is not None and rot is not None:
        raise BuildError(f"{where}: give one orientation, axis= or rot=, not both")
    if rot is not None:
        euler = _vec(where, "rot", rot)
    elif axis is None:
        euler = (0.0, 0.0, 0.0)
    elif isinstance(axis, str) and not upright:
        if axis not in AXIS_EULER:
            raise BuildError(f"{where}: axis must be \"x\", \"y\", \"z\" or a vector (x, y, z), got {axis!r}")
        euler = AXIS_EULER[axis]
    else:
        if isinstance(axis, str):
            if axis not in AXIS_EULER:
                raise BuildError(f"{where}: axis must be \"x\", \"y\", \"z\" or a vector (x, y, z), got {axis!r}")
            axis = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}[axis]
        d = np.asarray(_vec(where, "axis", axis))
        n = float(np.linalg.norm(d))
        if n < 1e-12:
            raise BuildError(f"{where}: axis is a zero vector; give the direction the shape's length points")
        d = d / n
        z = np.array([0.0, 0.0, 1.0])
        if upright and abs(d[2]) < 1.0 - 1e-9:
            y = z - d[2] * d
            y /= np.linalg.norm(y)
            x = np.cross(y, d)
            if x[0] < -1e-12 or (abs(x[0]) <= 1e-12 and x[1] < 0):
                x, d = -x, -d
            m = np.column_stack([x, y, d])
        elif d[2] < -1.0 + 1e-12:
            m = np.diag([1.0, -1.0, -1.0])  # straight down: half a turn about x
        else:
            v, c = np.cross(z, d), float(d[2])
            k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
            m = np.eye(3) + k + k @ k / (1.0 + c)
        euler = ir_shortcuts._euler(m)
    euler = tuple(_num0(x) for x in euler)
    return euler, ir_shortcuts._rot(euler)  # type: ignore[return-value]


def _mirror_axis(where: str, mirror) -> str | None:
    if mirror is None:
        return None
    if mirror not in AXES:
        raise BuildError(f"{where}: mirror must be \"x\", \"y\" or \"z\" (the plane through the origin across that "
                         f"axis), got {mirror!r}")
    return mirror
