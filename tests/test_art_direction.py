"""Named art directions (antigravity3d.art_direction): one source of truth per style, applied end to end.

* the style file's prose states exactly the values the pipeline reads (table and every number with a unit);
* no franchise, game, studio or product name reaches a style file, the skill or a model brief;
* a style only lowers budgets and never relaxes a gate;
* the package records and the validator checks what was applied; without a style nothing changes.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pxr import Usd, UsdGeom

from antigravity3d import art_direction as A
from antigravity3d.ir import MaterialIR, NodeIR, SceneIR, Transform
from antigravity3d.kernel.compile_ir import CREASE_DEG, crease_normals
from antigravity3d.kernel.fast_ops import run_export_pipeline
from antigravity3d.kernel.package import write_engine_package
from antigravity3d.kernel.usd_stage import StageManager
from antigravity3d.kernel.validator import _glb_json, validate_package
from antigravity3d.plugin_install import bundle_dir
from antigravity3d.state import EngineBudget

SKILL_DIR = bundle_dir() / "skills" / "art-direction"
STYLES = A.all_styles()
EIGHT = {"low-poly", "retro-lowres", "stylized", "realistic", "voxel", "isometric", "anime", "hand-painted"}

# Names harvested from the upstream files the styles were adapted from (arjun988/blender-skills@904f095: the eight
# style skills, their references, references/polycount-budgets.md, visual-console-eras, pixel-art, painterly and
# cartoon): games, studios, consoles and their nicknames, media formats that are trademarks, and the DCC product and
# feature names the upstream text instructs with. None may appear in a style file, the skill or a brief.
REMOVED_NAMES = (
    "Lethal Company", "Mouthwashing", "Fear to Fathom", "Puppet Combo", "Minecraft",
    "PlayStation", "PS1", "PS2", "PS3", "PS4", "PS5", "PSX", "Nintendo", "Switch",
    "VHS", "DOS",
    "Blender", "Eevee", "EEVEE", "Cycles", "Freestyle", "Grease Pencil", "Toon BSDF", "Shader to RGB", "ColorRamp",
    "Geometry Nodes", "Quad Remesh", "3D Print Toolbox", "Solidify", "Shrinkwrap", "MCP",
)

# "What the pipeline applies" table row label -> field
ROWS = {
    "Triangle budget, prop": "tri_budget_prop",
    "Triangle budget, kit piece": "tri_budget_kit_piece",
    "Shading crease angle": "shading_crease_deg",
    "Texture size, prop": "texture_px_prop",
    "Texture size, kit trim sheet": "texture_px_kit",
    "Texture filtering": "texture_filter",
    "Albedo palette": "palette_max_colors",
}
UNIT_FIELDS = {  # unit words in prose -> the fields whose value such a number must be
    "tris": ("tri_budget_prop", "tri_budget_kit_piece"), "triangles": ("tri_budget_prop", "tri_budget_kit_piece"),
    "deg": ("shading_crease_deg",), "degrees": ("shading_crease_deg",), "°": ("shading_crease_deg",),
    "px": ("texture_px_prop", "texture_px_kit"), "pixels": ("texture_px_prop", "texture_px_kit"),
    "colours": ("palette_max_colors",), "colors": ("palette_max_colors",),
}
_NUMBER_UNIT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(" + "|".join(map(re.escape, UNIT_FIELDS)) + r")(?![A-Za-z])")


def _table(style: A.ArtDirection) -> dict[str, str]:
    rows = {}
    for line in style.body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[0] in ROWS:
            assert cells[0] not in rows, f"{style.id}: row {cells[0]!r} twice"
            rows[cells[0]] = cells[1]
    return rows


def _setting(field: str, value) -> str:
    """How the table states a field's value."""
    if field.startswith("tri_budget"):
        return f"{value} tris" if value is not None else "engine budget"
    if field == "shading_crease_deg":
        return f"{value:g} deg" if value is not None else "project default"
    if field.startswith("texture_px"):
        return f"{value} px" if value is not None else "pipeline default"
    if field == "texture_filter":
        return value or "engine default"
    return f"{value} colours" if value is not None else "no limit"


# --------------------------------------------------------------------------- one source of truth


def test_the_eight_styles_load() -> None:
    assert set(STYLES) == EIGHT
    for s in STYLES.values():
        assert s.source == f"skills/art-direction/styles/{s.id}.md"
        assert s.sha256 == hashlib.sha256((SKILL_DIR / "styles" / f"{s.id}.md").read_bytes()).hexdigest()
        assert all(s.brief(k) for k in A.BRIEFS), f"{s.id}: every style briefs the author, the concept sheet and the textures"


@pytest.mark.parametrize("sid", sorted(EIGHT))
def test_prose_table_states_the_fields_the_pipeline_reads(sid: str) -> None:
    style = STYLES[sid]
    table = _table(style)
    assert set(table) == set(ROWS), f"{sid}: table rows {sorted(table)}"
    for label, field in ROWS.items():
        assert table[label] == _setting(field, getattr(style, field)), f"{sid}: {label!r} says {table[label]!r}, field {field} = {getattr(style, field)!r}"


@pytest.mark.parametrize("sid", sorted(EIGHT))
def test_every_number_with_a_unit_in_prose_and_briefs_is_a_field_value(sid: str) -> None:
    style = STYLES[sid]
    text = style.body + "\n" + "\n".join(style.briefs.values())
    for number, unit in _NUMBER_UNIT.findall(text):
        value = float(number.replace(",", ""))
        allowed = {float(getattr(style, f)) for f in UNIT_FIELDS[unit] if getattr(style, f) is not None}
        assert value in allowed, f"{sid}: prose says {number} {unit}, but the pipeline applies {sorted(allowed) or 'nothing'} for {unit}"


def test_number_check_catches_a_drift(tmp_path: Path) -> None:
    src = (SKILL_DIR / "styles" / "low-poly.md").read_text()
    drifted = tmp_path / "skills" / "art-direction" / "styles" / "low-poly.md"
    drifted.parent.mkdir(parents=True)
    drifted.write_text(src.replace("tri_budget_prop: 500", "tri_budget_prop: 450"))
    style = A.parse_style(drifted)
    assert style.tri_budget_prop == 450
    assert _table(style)["Triangle budget, prop"] != _setting("tri_budget_prop", 450)
    assert any(float(n) == 500 for n, u in _NUMBER_UNIT.findall(style.body) if u == "tris")


def test_style_files_are_validated(tmp_path: Path) -> None:
    d = tmp_path / "skills" / "art-direction" / "styles"
    d.mkdir(parents=True)
    src = (SKILL_DIR / "styles" / "low-poly.md").read_text()
    for bad, why in ((src.replace("texture_px_prop: 512", "texture_px_prop: 500"), "power of two"),
                     (src.replace("texture_filter: null", "texture_filter: bilinear"), "texture_filter"),
                     (src.replace("palette_max_colors: null", "palette_count: 8"), "unknown pipeline field"),
                     (src.replace("id: low-poly", "id: lowpoly"), "must equal the file name")):
        (d / "low-poly.md").write_text(bad)
        with pytest.raises(ValueError, match=why):
            A.parse_style(d / "low-poly.md")


# --------------------------------------------------------------------------- text hygiene and provenance


def _agent_texts() -> dict[str, str]:
    texts = {p.relative_to(SKILL_DIR).as_posix(): p.read_text() for p in sorted(SKILL_DIR.rglob("*.md"))}
    for s in STYLES.values():
        texts[f"briefs:{s.id}"] = "\n".join(s.briefs.values())
    return texts


@pytest.mark.parametrize("name", REMOVED_NAMES)
def test_no_franchise_studio_or_product_name(name: str) -> None:
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])")
    hits = [doc for doc, text in _agent_texts().items() if pattern.search(text)]
    assert not hits, f"{name!r} found in {hits}"


def test_console_nickname_only_as_an_alias_token() -> None:
    """``psx`` (an alias the owner types) may appear only as the exact backticked alias token, nowhere else, and no
    brief carries it; the alias table in code is the only place it is defined."""
    assert A.ALIASES == {"psx": "retro-lowres"}
    for doc, text in _agent_texts().items():
        stripped = text
        for alias in A.ALIASES:
            stripped = stripped.replace(f"`{alias}`", "")
        assert not re.search(r"psx", stripped, re.I), f"console nickname outside the alias token in {doc}"
        if doc.startswith("briefs:"):
            assert "psx" not in text.lower()


def test_aliases_resolve_to_the_canonical_id(tmp_path: Path) -> None:
    from antigravity3d.harness.session import open_session, session
    from antigravity3d.state import SceneSessionRegistry

    assert A.load("psx") is STYLES["retro-lowres"] or A.load("PSX") == STYLES["retro-lowres"]
    reg = SceneSessionRegistry()
    open_session(reg, "s", tmp_path / "s", "Crate", EngineBudget("unreal_5"), plane=None, forge=None, art_direction="psx")
    assert session(reg, "s").state.art_direction == "retro-lowres"
    with pytest.raises(A.UnknownArtDirection, match="aliases: psx = retro-lowres"):
        A.load("ps9")


def test_provenance_record() -> None:
    prov = json.loads((SKILL_DIR / "art-direction.provenance.json").read_text())
    assert prov["commit"] == "904f095d8f36a6e72adaebb08d07f884a074743d" and prov["license"]["spdx"] == "MIT"
    lic = (SKILL_DIR / prov["license"]["file"]).read_bytes()
    assert hashlib.sha256(lic).hexdigest() == prov["license"]["sha256"] and b"Permission is hereby granted" in lic
    assert set(prov["styles"]) == EIGHT
    for sid, rec in prov["styles"].items():
        assert rec["files"] and all(re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) for f in rec["files"].values()), sid
    upstream = Path("/home/user/arjun988/blender-skills/.claude/skills")
    if upstream.is_dir():  # the upstream checkout this was adapted from, when present: every recorded hash still holds
        for rec in prov["styles"].values():
            for rel, f in rec["files"].items():
                assert hashlib.sha256((upstream / rel).read_bytes()).hexdigest() == f["sha256"], rel


# --------------------------------------------------------------------------- applied values never loosen a gate


@pytest.mark.parametrize("sid", sorted(EIGHT))
@pytest.mark.parametrize("engine", ["unreal_5", "roblox_r15"])
def test_a_style_never_raises_a_budget_or_a_texture_limit(sid: str, engine: str) -> None:
    from antigravity3d.kernel.validator import ENGINE_RULES

    style = STYLES[sid]
    for max_tris in (100, 15_000, 60_000):
        base = EngineBudget(engine, max_tris=max_tris)  # type: ignore[arg-type]
        for cls, field in (("prop", "tri_budget_prop"), ("kit", "tri_budget_kit_piece")):
            eff = style.budget(base, cls)  # type: ignore[arg-type]
            # a style lowers two limits and nothing else: max_tris, and (since one atlas holds every opaque material)
            # max_texture_px, the largest side of that atlas, from its texture size
            assert eff.max_tris <= max_tris and replace(eff, max_tris=max_tris, max_texture_px=None) == base
            cap = getattr(style, field)
            assert eff.max_tris == (min(cap, max_tris) if cap else max_tris)
            px = style.texture_px_prop if cls == "prop" else style.texture_px_kit
            assert eff.max_texture_px == px
    for cls in ("prop", "kit"):
        assert style.texture_px(engine, cls, 2048) <= ENGINE_RULES[engine].max_texture_px  # type: ignore[arg-type]


def test_unknown_id_lists_the_valid_ones() -> None:
    with pytest.raises(A.UnknownArtDirection, match="valid ids: anime, hand-painted, isometric, low-poly, realistic, retro-lowres"):
        A.load("lowpoly")
    assert A.load("") is None and A.load(None) is None


def test_finish_map_sizes_quantises_and_renormalises() -> None:
    rng = np.random.default_rng(3)
    albedo = Image.fromarray(rng.integers(30, 240, (256, 256, 3), dtype=np.uint8), "RGB")
    retro = STYLES["retro-lowres"]
    out = retro.finish_map(albedo, "albedo", 128)
    assert out.size == (128, 128) and A.count_colors(out) <= retro.palette_max_colors
    assert retro.finish_map(albedo, "albedo", 128).tobytes() == out.tobytes()  # deterministic
    n = rng.normal(size=(256, 256, 3)) * 0.3 + [0, 0, 1]
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    normal = Image.fromarray(np.rint((n + 1) * 127.5).astype(np.uint8), "RGB")
    small = np.asarray(retro.finish_map(normal, "normal", 128), np.float64) / 127.5 - 1
    assert np.allclose(np.linalg.norm(small, axis=-1), 1.0, atol=0.02)
    assert A.count_colors(retro.finish_map(normal, "normal", 128)) > retro.palette_max_colors  # palette is albedo only
    assert STYLES["stylized"].finish_map(albedo, "albedo", None) is albedo  # nothing to do: the same image


# --------------------------------------------------------------------------- the package records and checks it


def _crate(root: Path) -> tuple[StageManager, object]:
    root.mkdir(parents=True)
    rng = np.random.default_rng(7)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(40, 220, (256, 256, 3), dtype=np.uint8), "RGB").save(buf, "PNG")
    (root / "wood_albedo.png").write_bytes(buf.getvalue())
    scene = SceneIR(asset_name="Crate", nodes=[
        NodeIR(id="body", kind="box", params={"size": [1, 0.8, 0.6]}, transform=Transform(translate=(0, 0, 0.3)), material_id="wood"),
        NodeIR(id="post", kind="cylinder", params={"radius": 0.1, "height": 0.5, "segments": 12},
               transform=Transform(translate=(0, 0, 0.85)), material_id="wood"),
    ], materials=[MaterialIR(id="wood", base_color=(0.5, 0.35, 0.2), albedo_uri=str(root / "wood_albedo.png"))])
    sm = StageManager.create(root / "Crate_stage.usda", "Crate")
    sm.apply_ir(scene)
    return sm, run_export_pipeline(sm.extract_meshes(), EngineBudget("godot_4"), "Crate")


def _glb_normals(path: Path) -> np.ndarray:
    import pygltflib

    g = pygltflib.GLTF2().load(str(path))
    blob = g.binary_blob()
    out = []
    for mesh in g.meshes:
        acc = g.accessors[mesh.primitives[0].attributes.NORMAL]
        view = g.bufferViews[acc.bufferView]
        out.append(np.frombuffer(blob[view.byteOffset:view.byteOffset + view.byteLength], np.float32).reshape(-1, 3))
    return np.vstack(out)


def test_a_part_that_lost_its_hidden_faces_keeps_the_art_directions_crease(tmp_path: Path) -> None:
    """The post stands exactly on the body, so its bottom cap is a hidden contact face and is not shipped. The normals
    rewritten for the part's remaining triangles must be the art direction's (0 degrees here, hard edges), not the
    default's: the art_crease check of the package compares them with crease_normals at the shipped crease."""
    from antigravity3d.kernel import coplanar

    retro = STYLES["retro-lowres"]
    budget = retro.budget(EngineBudget("godot_4"), "prop")
    sm, _ = _crate(tmp_path / "a")
    scene = sm.base_ir()
    bundle = run_export_pipeline(sm.extract_meshes(), budget, "Crate", opaque_materials=coplanar.opaque_materials(scene))
    pkg = write_engine_package(sm, bundle, budget, tmp_path / "a" / "pkg", asset_name="Crate", art=retro)
    man = json.loads((pkg.out_dir / "manifest.json").read_text())
    assert man["contact_faces"]["removed_per_part"] == {"post": 12}  # the post's bottom cap (the body's top is only partly covered)
    assert pkg.ok, [i for i in pkg.issues if i.severity == "critical"]
    assert not [i for i in pkg.issues if i.code == "pkg:art_crease"]


def test_package_records_and_validates_an_art_direction(tmp_path: Path) -> None:
    retro = STYLES["retro-lowres"]
    budget = retro.budget(EngineBudget("godot_4"), "prop")
    sm, bundle = _crate(tmp_path / "a")
    pkg = write_engine_package(sm, bundle, budget, tmp_path / "a" / "pkg", asset_name="Crate", art=retro)
    man = json.loads((pkg.out_dir / "manifest.json").read_text())
    assert pkg.ok, [i for i in pkg.issues if i.severity == "critical"]
    assert man["schema_version"] == 4 and man["art_direction"]["id"] == "retro-lowres"  # schema 4: the provenance record (kernel/provenance.py)
    assert man["art_direction"]["sha256"] == retro.sha256
    assert man["art_direction"]["effective"] == {"max_tris": 400, "shading_crease_deg": 0.0, "texture_px": 128,
                                                 "texture_filter": "nearest", "palette_max_colors": 64}
    assert man["budget"]["max_tris"] == 400 and man["uv"]["normals"] == "faceVarying (crease 0 deg)"
    td = man["texel_density"]  # one truth: atlas-space figure labelled, the shipped one beside it
    assert td["space"].startswith("atlas") and td["shipped_texture_px"] == 128
    assert td["shipped_mean_px_per_m"] == pytest.approx(td["mean_px_per_m"] * 128 / 2048, rel=1e-3)
    for glb in [f for f in man["files"] if f.endswith(".glb") and "colliders" not in f]:
        doc = _glb_json(pkg.out_dir / glb)
        assert doc["samplers"] == [{"magFilter": 9728, "minFilter": 9984, "wrapS": 10497, "wrapT": 10497}], glb
        assert all(t["sampler"] == 0 for t in doc["textures"])
    # faceted: every GLB normal is a face normal (the cylinder side would be smooth at the project's 40 deg)
    smooth = _glb_normals(tmp_path / "a" / "pkg" / "Crate.glb")
    assert len(np.unique(np.round(smooth, 4), axis=0)) <= 6 + 2 + 12  # box faces + caps + one per cylinder side
    assert validate_package(pkg.out_dir) == []

    # the validator compares the files with the record: tampering is caught
    for key, value, code in (("texture_filter", None, "pkg:art_texture_filter"), ("shading_crease_deg", 40.0, "pkg:art_crease"),
                             ("max_tris", 15000, "pkg:art_budget")):
        bad = json.loads(json.dumps(man))
        bad["art_direction"]["effective"][key] = value
        (pkg.out_dir / "manifest.json").write_text(json.dumps(bad))
        assert code in {i.code for i in validate_package(pkg.out_dir)}, key
    # the record is optional in version 3 (absent: no style), but a version 2 manifest cannot carry it
    bad = json.loads(json.dumps(man))
    del bad["art_direction"]
    (pkg.out_dir / "manifest.json").write_text(json.dumps(bad))
    assert not {i.code for i in validate_package(pkg.out_dir)} & {"pkg:art_direction_manifest", "pkg:art_budget"}
    bad = json.loads(json.dumps(man))
    bad["schema_version"] = 2
    (pkg.out_dir / "manifest.json").write_text(json.dumps(bad))
    assert "pkg:art_direction_manifest" in {i.code for i in validate_package(pkg.out_dir)}


def test_package_without_art_direction_is_unchanged(tmp_path: Path) -> None:
    budget = EngineBudget("godot_4")
    sm, bundle = _crate(tmp_path / "a")
    pkg = write_engine_package(sm, bundle, budget, tmp_path / "a" / "pkg", asset_name="Crate")
    man = json.loads((pkg.out_dir / "manifest.json").read_text())
    assert man["schema_version"] == 4 and "art_direction" not in man and man["uv"]["normals"] == "faceVarying (crease 40 deg)"  # schema 4: the provenance record (kernel/provenance.py)
    assert "samplers" not in _glb_json(pkg.out_dir / "Crate.glb")
    sm2, bundle2 = _crate(tmp_path / "b")
    pkg2 = write_engine_package(sm2, bundle2, budget, tmp_path / "b" / "pkg", asset_name="Crate", art=None)
    a = {p.relative_to(pkg.out_dir): p.read_bytes() for p in pkg.out_dir.rglob("*") if p.is_file()}
    b = {p.relative_to(pkg2.out_dir): p.read_bytes() for p in pkg2.out_dir.rglob("*") if p.is_file()}
    assert a == b


def test_usd_normals_follow_the_crease(tmp_path: Path) -> None:
    sm, bundle = _crate(tmp_path / "a")
    write_engine_package(sm, bundle, EngineBudget("godot_4"), tmp_path / "a" / "pkg", asset_name="Crate", art=STYLES["low-poly"])
    stage = Usd.Stage.Open(str(tmp_path / "a" / "pkg" / "Crate.usd"))
    post = UsdGeom.Mesh(next(p for p in stage.Traverse() if p.GetName() == "post"))
    pts = np.asarray(post.GetPointsAttr().Get(), np.float32)
    tri = np.asarray(post.GetFaceVertexIndicesAttr().Get()).reshape(-1, 3)
    have = np.asarray(post.GetNormalsAttr().Get())
    assert np.allclose(have, crease_normals(pts, tri, 0.0), atol=1e-5)
    assert not np.allclose(have, crease_normals(pts, tri, CREASE_DEG), atol=1e-3)


def test_export_cache_key_follows_what_the_bundle_depends_on(tmp_path: Path) -> None:
    """The cached UV/LOD/collider bundle depends on the scene and the budget; a style reaches it only through the
    budget it lowers (its crease, maps and sampler are applied after the bundle, from the uncached style), so a style
    that keeps the budget shares the bundle and one that lowers it gets its own."""
    from antigravity3d.tactics.cached import export_key

    sm, _ = _crate(tmp_path / "a")
    scene, base = sm.scene_ir(), EngineBudget("godot_4")
    assert export_key(scene, STYLES["anime"].budget(base, "prop")) == export_key(scene, base)
    assert export_key(scene, STYLES["voxel"].budget(base, "prop")) == export_key(scene, base)  # crease/sampler only
    assert export_key(scene, STYLES["low-poly"].budget(base, "prop")) != export_key(scene, base)


# --------------------------------------------------------------------------- model calls


def test_briefs_reach_the_author_request_only_with_a_style() -> None:
    from antigravity3d.tactics.metered_author import build_author_request

    budget = EngineBudget("unreal_5")

    def text(context: dict) -> str:
        req = build_author_request("worker", "propose", "Build design 1/1 for a crate.", budget=budget, context=context,
                                   timeout_s=30, asset_name="Crate")
        return "".join(p.text or "" for c in req.contents for p in c.parts)

    plain = text({"asset_name": "Crate"})
    styled = text({"asset_name": "Crate", "art_direction": A.author_text(STYLES["low-poly"])})
    assert styled == plain + "\n" + A.author_text(STYLES["low-poly"])
    assert A.author_text(None) == "" and A.concept_prompt("crate", None) == "crate" and A.texture_brief(None) == ""


def test_stage_agents_get_the_brief_only_with_a_style() -> None:
    from antigravity3d.harness.agents import _system

    low = STYLES["low-poly"]
    assert _system("BLOCKOUT") == _system("BLOCKOUT", art=None)
    assert _system("BLOCKOUT", art=low) == _system("BLOCKOUT").replace("\nBe terse.", f"\n{A.author_text(low)}\nBe terse.")
    assert low.brief("texture") in _system("LOOKDEV", art=low) and low.brief("author") not in _system("LOOKDEV", art=low)
    assert _system("EXPORT", art=low) == _system("EXPORT")


def test_session_carries_the_art_direction(tmp_path: Path) -> None:
    from antigravity3d.harness.hooks import state_header
    from antigravity3d.harness.session import open_session, session
    from antigravity3d.state import SceneSessionRegistry

    reg = SceneSessionRegistry()
    open_session(reg, "plain", tmp_path / "p", "Crate", EngineBudget("unreal_5"), plane=None, forge=None)
    open_session(reg, "styled", tmp_path / "s", "Crate", EngineBudget("unreal_5"), plane=None, forge=None, art_direction="retro-lowres")
    plain, styled = session(reg, "plain"), session(reg, "styled")
    assert plain.art is None and "art=" not in state_header(plain)
    assert styled.art is STYLES["retro-lowres"] or styled.art == STYLES["retro-lowres"]
    assert styled.state.budget.max_tris == 400 and state_header(styled).endswith("art=retro-lowres]")
    with pytest.raises(A.UnknownArtDirection):
        open_session(reg, "bad", tmp_path / "b", "Crate", EngineBudget("unreal_5"), plane=None, forge=None, art_direction="ps9")
    assert "bad" not in reg and not (tmp_path / "b").exists()


def test_mcp_open_asset_session_refuses_an_unknown_id_before_creating_anything(tmp_path: Path) -> None:
    from antigravity3d.runtime.mcp_server import StudioHost

    host = StudioHost()
    with pytest.raises(A.UnknownArtDirection, match="valid ids"):
        asyncio.run(host.open_asset_session("Crate", "unreal_5", workspace=str(tmp_path), art_direction="toon"))
    assert not host.gates and not any(tmp_path.iterdir())


def test_brief_token_cost_is_small() -> None:
    """Every brief stays short: it is input on every call it is added to."""
    for s in STYLES.values():
        assert A.estimate_tokens(A.author_text(s)) <= 90 and A.estimate_tokens(s.brief("concept")) <= 45
        assert A.estimate_tokens(s.brief("texture")) <= 50, s.id


# --------------------------------------------------------------------------- end to end, offline


def _agy(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "antigravity3d.cli", *args], capture_output=True, text=True, timeout=600)


def test_cli_unknown_art_direction_is_a_usage_error_before_anything_runs(tmp_path: Path) -> None:
    for cmd in (["prop", "crate", "--offline"], ["kit", "temple", "--offline"]):
        r = _agy(*cmd, "--out", str(tmp_path / "o"), "--art-direction", "ps9")
        assert r.returncode == 2 and "valid ids: anime, hand-painted" in r.stderr
    assert not (tmp_path / "o").exists()


def test_offline_prop_with_art_direction_and_style_anchor(tmp_path: Path) -> None:
    anchor = tmp_path / "anchor.png"
    Image.new("RGB", (64, 64), (120, 90, 60)).save(anchor)
    r = _agy("prop", "oak barrel with iron hoops", "--offline", "--engine", "godot_4", "--out", str(tmp_path / "o"),
             "--art-direction", "psx", "--style", str(anchor), "--no-cache")  # the alias: only the canonical id ships
    assert r.returncode == 0, r.stdout + r.stderr
    report = json.loads((tmp_path / "o" / "report.json").read_text())
    assert report["comparison"]["style_sha256"] == hashlib.sha256(anchor.read_bytes()).hexdigest()
    assert report["comparison"]["art_direction"] == "retro-lowres" and report["art_direction"]["effective"]["max_tris"] == 400
    pkg = Path(report["outputs"]["inspector"]).parent
    man = json.loads((pkg / "manifest.json").read_text())
    assert man["schema_version"] == 4 and man["tris"] <= 400 and man["validation"]["package_ok"]  # schema 4: the provenance record (kernel/provenance.py)
    assert {t["width"] for t in man["textures"]} == {128}
    assert all(n <= 64 for n in man["art_direction"]["measured"]["albedo_colors"].values())
    assert validate_package(pkg) == []


def test_offline_prop_without_art_direction_runs_no_style_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no art direction, no style code runs and every kernel default is the one used before styles existed:
    the crease angle of every normal written is the project's 40 degrees, no sampler is written, maps keep the
    offline size and the manifest carries no art_direction record."""
    import antigravity3d.kernel.compile_ir as ci
    from antigravity3d.cli import main

    def forbidden(*_a, **_k):
        raise AssertionError("art-direction code ran without an art direction")

    for name in ("budget", "crease_deg", "texture_px", "sampler", "finish_map", "brief", "manifest_record"):
        monkeypatch.setattr(A.ArtDirection, name, forbidden)
    creases: set[float] = set()
    real = ci.crease_normals
    # crease_normals also takes a part's run (round seven: round parts ship smooth); the crease angle is what is recorded
    monkeypatch.setattr(ci, "crease_normals", lambda p, t, crease_deg=CREASE_DEG, *a, **k: creases.add(crease_deg) or real(p, t, crease_deg, *a, **k))
    out = tmp_path / "o"
    assert main(["prop", "wooden crate with iron brackets", "--offline", "--engine", "unreal_5", "--out", str(out), "--no-cache"]) == 0
    report = json.loads((out / "report.json").read_text())
    assert "art_direction" not in report and "art_direction" not in report["comparison"]
    pkg = Path(report["outputs"]["inspector"]).parent
    man = json.loads((pkg / "manifest.json").read_text())
    # schema 4: the provenance record (kernel/provenance.py)
    assert man["schema_version"] == 4 and "art_direction" not in man and man["uv"]["normals"] == "faceVarying (crease 40 deg)"
    # the offline maps are 256 px; the opaque materials ship as one atlas baked from them at the density they hold
    # (package._compose_atlas), every texel copied 1:1: no style resized anything
    atlas = man["texture_atlas"]
    assert {r["resample_ratio"] for m in atlas["members"].values() for r in m["maps"].values()} == {1.0}
    assert {t["width"] for t in man["textures"]} == {atlas["atlas_wh"][0]}
    assert all("samplers" not in _glb_json(pkg / f) for f in man["files"] if f.endswith(".glb"))
    assert creases == {CREASE_DEG}


def test_offline_kit_with_art_direction(tmp_path: Path) -> None:
    r = _agy("kit", "ruined stone temple", "--pieces", "2", "--offline", "--out", str(tmp_path / "k"), "--art-direction", "retro-lowres")
    assert r.returncode == 0, r.stdout + r.stderr
    report = json.loads((tmp_path / "k" / "report.json").read_text())
    art = report["art_direction"]
    assert art["effective"]["max_tris"] == 2000 and art["effective"]["texture_px"] == 512 and art["not_applied"]
    with Image.open(tmp_path / "k" / "trim_sheet.png") as im:
        assert im.size == (512, 512) and A.count_colors(im) <= 64
    piece = Usd.Stage.Open(report["pieces"][next(iter(report["pieces"]))])
    meshes = [UsdGeom.Mesh(p) for p in piece.Traverse() if p.IsA(UsdGeom.Mesh) and not p.GetName().startswith("UCX_")]
    assert meshes and all(m.GetNormalsAttr().Get() is not None for m in meshes)


# --------------------------------------------------------------------------- engines (stock Godot, three.js)


@pytest.mark.browser
def test_styled_package_imports_in_stock_godot_and_passes_the_web_check(tmp_path: Path) -> None:
    """A faceted, nearest-sampled, 128 px, 64-colour package (retro-lowres) is still a package engines take: stock Godot
    imports the GLB with every map bound, and the three.js web check (load, silhouette against the USD) passes."""
    from antigravity3d.adapters.engines.validators import GodotValidator
    from antigravity3d.adapters.engines.web_three import WebThreeValidator

    godot, web = GodotValidator(), WebThreeValidator()
    if not godot.available() or not web.available():
        pytest.skip("needs GODOT_BIN and Playwright Chromium (CI's stock-engines job)")
    retro = STYLES["retro-lowres"]
    sm, bundle = _crate(tmp_path / "a")
    pm = write_engine_package(sm, bundle, retro.budget(EngineBudget("godot_4"), "prop"), tmp_path / "a" / "pkg",
                              asset_name="Crate", art=retro)
    assert pm.ok and validate_package(pm.out_dir) == []
    report = asyncio.run(godot.validate(pm.out_dir / "Crate.glb", timeout=60))
    assert report.ok, report.log_tail
    assert report.metrics["triangles"] == pm.data["tris"] and report.metrics["textured_surfaces"] == report.metrics["surfaces"]
    assert report.metrics["images_loaded"] == report.metrics["images"]
    rep = asyncio.run(web.validate(pm.out_dir, 60, out_dir=tmp_path / "web"))
    assert rep.ok, rep.summary()
