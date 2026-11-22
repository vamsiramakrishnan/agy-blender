"""The manifest says what made each shipped file (``kernel/provenance.py``, ``schema_version`` 4).

Pinned here:
* the structure: every listed file has an entry, every dependency resolves, the package-level answers are what the
  graph gives, and ``validate_package`` rejects a record that is not (offline, real kernel);
* the answer to "does this file contain generated content": a model-authored design or a model-generated image makes
  every file derived from it so, a template, the offline stand-in and a file the user supplied do not, and a source
  nobody recorded makes the answer ``None``, never ``False``;
* determinism: two offline runs of one request give byte-identical manifests, record included;
* reuse: a rerun that takes the design from the design record and the images from the image cache (round four) records
  the same provenance as the run that paid for them, with no ``(cached)`` mark and no cost or time in it;
* a native session credits an albedo to the image model only while the material still uses the maps it wrote.

No live call, no key (``tests/no_decisions_key.py`` stands in for the service).
"""

from __future__ import annotations

import copy
import glob
import json
import sys
from pathlib import Path

import pytest

from antigravity3d import cli
from antigravity3d.kernel import provenance
from antigravity3d.kernel.validator import validate_package

sys.path.insert(0, str(Path(__file__).parent))
from no_decisions_key import NoDecisionsKey  # noqa: E402

PREVIEW = ["--preview", "--token-ceiling", "600000", "--max-images", "6", "--single-candidate"]  # room on the shared ledger for the rerun


def prop(out: Path, *args: str) -> int:
    return cli.main(["prop", "wooden crate with iron brackets", "--out", str(out), *args])


def manifest_of(out: Path) -> dict:
    """The manifest of the latest run in ``out`` (each run writes its own folder under ``runs/``)."""
    found = sorted(glob.glob(str(out / "runs" / "*" / "package" / "manifest.json")), key=lambda p: Path(p).stat().st_mtime)
    assert found, f"no package under {out}"
    return json.loads(Path(found[-1]).read_text())


def packages(out: Path) -> list[Path]:
    return [Path(p).parent for p in glob.glob(str(out / "runs" / "*" / "package" / "manifest.json"))]


# --------------------------------------------------------------------------- the graph


def record() -> dict:
    data = {"asset": "Prop", "inspector": "inspector.html",
            "files": ["Prop.usd", "Prop.usdz", "Prop.glb", "Prop_colliders.glb", "inspector.html", "textures/paint_albedo.png",
                      "textures/paint_orm.png", "textures/paint_normal.png"],
            "textures": [{"material": "paint", "role": "albedo", "path": "textures/paint_albedo.png", "usd_path": "textures/paint_albedo.png"},
                         {"material": "paint", "role": "orm", "path": "textures/paint_orm.png", "usd_path": "textures/paint_orm.png"},
                         {"material": "paint", "role": "normal", "path": "textures/paint_normal.png", "usd_path": "textures/paint_normal.png",
                          "converted": True}]}
    return data, provenance.finalize(provenance.build(data, asset="Prop"))


def test_an_unrecorded_source_is_unknown_never_no() -> None:
    _, prov = record()
    assert prov["generated_content"] is None and prov["files"]["Prop.glb"]["generated_content"] is None
    assert prov["unrecorded"] == ["albedo:paint", "design"]


def test_a_model_makes_every_file_derived_from_its_output_generated() -> None:
    data, prov = record()
    provenance.set_design(prov, {"kind": "template", "generator": "t"})
    provenance.set_image(prov, "albedo:paint", {"kind": "model_generated_image", "model": "img-model", "prompt": "olive paint"})
    provenance.finalize(prov, data)
    assert prov["generated_content"] is True and prov["models"] == ["img-model"] and prov["unrecorded"] == []
    assert prov["files"]["textures/paint_orm.png"]["generated_by"] == ["img-model"]
    assert prov["files"]["Prop.glb"]["generated_content"] is True  # it embeds the generated maps
    assert prov["files"]["Prop_colliders.glb"]["generated_content"] is False  # a hull of the template's design only
    assert prov["files"]["Prop.usdz"]["generated_content"] is True  # through Prop.usd


def test_a_template_the_stand_in_and_the_users_own_file_are_not_generated_content() -> None:
    data, prov = record()
    provenance.set_design(prov, {"kind": "user_supplied"})
    provenance.set_image(prov, "albedo:paint", {"kind": "offline_fake_image", "model": "offline fake image backend"})
    provenance.finalize(prov, data)
    assert prov["generated_content"] is False and prov["offline_fakes"] is True and prov["models"] == []


def test_a_normal_map_flip_and_the_files_added_later_are_recorded() -> None:
    data, prov = record()
    assert prov["files"]["textures/paint_normal.png"]["steps"] == ["prepare_albedo", "derive_pbr", "normal_convention_flip"]
    data["files"] += ["ortho.png", "engine.png", "Prop.fbx"]
    provenance.finalize(prov, data)
    assert prov["files"]["ortho.png"]["derived_from"] == ["ortho"] and prov["sources"]["ortho"] == {"kind": "unrecorded"}
    assert prov["files"]["Prop.fbx"]["steps"] == ["blender_fbx_export"] and "Prop.glb" in prov["files"]["engine.png"]["derived_from"]
    data["files"].remove("Prop.fbx")  # a dropped FBX leaves the record
    provenance.finalize(prov, data)
    assert "Prop.fbx" not in prov["files"]


def test_the_checker_flags_what_the_record_gets_wrong() -> None:
    data, prov = record()
    man = {**data, "provenance": copy.deepcopy(prov)}
    assert provenance.problems(man) == []
    man["files"] = [*man["files"], "extra.bin"]
    assert provenance.problems(man) == ["extra.bin: no provenance entry"]
    man = {**data, "provenance": copy.deepcopy(prov)}
    man["provenance"]["files"]["Prop.glb"]["derived_from"].append("nowhere")
    assert "neither a source nor a file" in provenance.problems(man)[0]
    man = {**data, "provenance": copy.deepcopy(prov)}
    man["provenance"]["files"]["Prop.glb"]["generated_content"] = False  # the graph says unknown
    assert "generated_content is False" in provenance.problems(man)[0]
    assert provenance.problems({"files": []}) == ["manifest has no provenance record"]


def test_a_supplied_mesh_is_named_by_hash_and_file_name_only(tmp_path: Path) -> None:
    from antigravity3d.ir import NodeIR, SceneIR

    mesh = tmp_path / "private_dir" / "tree.glb"
    mesh.parent.mkdir()
    mesh.write_bytes(b"glTF-not-really")
    found = provenance.supplied_meshes(SceneIR(asset_name="T", nodes=[NodeIR(id="src", kind="mesh_ref", params={"path": str(mesh)})]))
    assert found == {"supplied:tree.glb": {"kind": "user_supplied", "sha256": provenance.sha256_file(mesh), "bytes": 15}}
    assert "private_dir" not in json.dumps(found)


# --------------------------------------------------------------------------- real packages


def test_an_offline_package_accounts_for_every_file_and_two_runs_are_byte_identical(tmp_path: Path) -> None:
    for name in ("a", "b"):
        assert cli.main(["prop", "a rugged supply crate", "--offline", "--engine", "godot_4", "--out", str(tmp_path / name)]) == 0
    one, two = (packages(tmp_path / n)[0] / "manifest.json" for n in ("a", "b"))
    assert one.read_bytes() == two.read_bytes()
    man = json.loads(one.read_text())
    assert man["schema_version"] == 4 and man["validation"]["package_ok"]
    prov = man["provenance"]
    assert set(prov["files"]) == set(man["files"]) - {"manifest.json"} and provenance.problems(man) == []
    assert prov["sources"]["design"]["kind"] == "template"
    assert prov["offline_fakes"] is True and prov["generated_content"] is False and prov["unrecorded"] == []
    assert not [i for i in validate_package(one.parent) if i.severity == "critical"]
    assert not any(w in one.read_text() for w in ("(cached)", str(tmp_path)))  # no cache mark, no local path
    # the validator holds the record to the graph: a file with its entry removed is a critical issue
    del man["provenance"]["files"]["RuggedSupplyCrate.glb"]
    one.write_text(json.dumps(man))
    assert [i.code for i in validate_package(one.parent) if i.severity == "critical" and i.code == "pkg:provenance"]


@pytest.fixture
def key(monkeypatch):
    def start(**kw):
        api = NoDecisionsKey(**kw).__enter__()
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", api.url)
        monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
        monkeypatch.delenv("AGY3D_THREE_JS", raising=False)
        started.append(api)
        return api

    started: list[NoDecisionsKey] = []
    yield start
    for api in started:
        api.__exit__(None, None, None)


def test_a_rerun_from_the_design_record_and_the_image_cache_records_what_the_paying_run_did(key, tmp_path: Path, capsys) -> None:
    out = tmp_path / "out"
    first = key()
    assert prop(out, *PREVIEW) == 0
    paid = manifest_of(out)["provenance"]
    assert sum(k == "author:propose" for k in first.kinds()) == 1 and sum(k == "image:sent" for k in first.kinds()) >= 1
    again = key()
    assert prop(out, *PREVIEW) == 0
    capsys.readouterr()
    assert not any(k in ("author:propose", "image:sent") for k in again.kinds())  # nothing was bought the second time
    assert len(packages(out)) == 2
    reused = manifest_of(out)["provenance"]
    assert reused == paid
    design = paid["sources"]["design"]
    assert design["kind"] == "model_authored" and design["model"] and len(design["request_sha256"]) == 64 and len(design["scene_sha256"]) == 64
    images = [s for k, s in paid["sources"].items() if k.startswith("albedo:")]
    assert images and all(s["kind"] == "model_generated_image" and s["model"] and "(cached)" not in s["model"] and len(s["image_sha256"]) == 64
                          for s in images)
    assert paid["generated_content"] is True and design["model"] in paid["models"] and paid["unrecorded"] == []
    assert "(cached)" not in json.dumps(paid)

    fields = set(paid) | {k for s in paid["sources"].values() for k in s} | {k for f in paid["files"].values() for k in f}
    assert not {k for k in fields if any(w in k for w in ("usd", "token", "cost", "latency", "booked", "time"))}  # identities, no run facts


def test_the_request_digest_is_the_design_records_key_without_its_prefix(tmp_path: Path) -> None:
    from antigravity3d.kernel.cache import BuildCache, LocalBlobStore
    from antigravity3d.state import EngineBudget
    from antigravity3d.tactics.cached import DesignRecord, request_digest
    from antigravity3d.tactics.metered_author import build_author_request, propose_task

    budget = EngineBudget("godot_4")
    req = build_author_request("builder", "propose", propose_task("a crate", index=0, n=1, asset_name="Crate", references=False),
                               budget=budget, context={"asset_name": "Crate"}, timeout_s=60.0, asset_name="Crate")
    record = DesignRecord(BuildCache(LocalBlobStore(tmp_path)))
    assert record.key(req, 0) == "design-" + request_digest(req, 0, record.cache.upload_digest)
    assert request_digest(req, 0) != request_digest(req, 1)


def test_the_native_session_hook_credits_an_albedo_only_to_the_maps_it_wrote(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from antigravity3d.harness import tools as T
    from antigravity3d.ir import MaterialIR, NodeIR, SceneIR

    maps = tmp_path / "materials" / "paint_v1"
    maps.mkdir(parents=True)
    scene = SceneIR(asset_name="P", nodes=[NodeIR(id="b", kind="box", params={"size": (1, 1, 1)}, material_id="paint")],
                    materials=[MaterialIR(id="paint", albedo_uri=str(maps / "albedo.png"))])
    media = {"paint": {"dir": str(maps), "kind": "model_generated_image", "model": "img-model", "prompt": "olive paint"}}
    session = SimpleNamespace(stage=SimpleNamespace(base_ir=lambda: scene), _res=lambda name, default=None: media)
    data = {"asset": "P", "inspector": "inspector.html", "files": ["P.glb", "inspector.html", "textures/albedo.png"],
            "textures": [{"material": "paint", "role": "albedo", "path": "textures/albedo.png", "usd_path": "textures/albedo.png"}]}
    data["provenance"] = provenance.finalize(provenance.build(data, asset="P"))
    T._session_provenance(session, data)
    prov = data["provenance"]
    assert prov["sources"]["design"]["kind"] == "model_authored_in_session" and prov["sources"]["design"]["model"] is None
    assert prov["sources"]["albedo:paint"]["model"] == "img-model" and "dir" not in prov["sources"]["albedo:paint"]
    scene.materials[0] = MaterialIR(id="paint", albedo_uri=str(tmp_path / "elsewhere" / "albedo.png"))  # the user's own map now
    data["provenance"] = provenance.finalize(provenance.build(data, asset="P"))
    T._session_provenance(session, data)
    assert data["provenance"]["sources"]["albedo:paint"] == {"kind": "unrecorded"}


def test_the_documented_shape_names_every_source_kind_and_top_level_key() -> None:
    doc = (Path(__file__).resolve().parent.parent / "docs" / "package_provenance.md").read_text()
    for kind in sorted(provenance.MODEL_KINDS | provenance.NON_MODEL_KINDS | {"unrecorded"}):
        assert f"`{kind}`" in doc, kind
    data = {"asset": "P", "inspector": "i.html", "files": ["P.glb"], "textures": []}
    for key in provenance.finalize(provenance.build(data, asset="P")):
        assert f"`{key}`" in doc or f'"{key}"' in doc, key
