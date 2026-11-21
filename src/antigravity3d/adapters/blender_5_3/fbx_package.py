"""The FBX of a package, written by the Blender worker: one helper for the prop pipeline (pass C) and the native export.

Same contract as ``upstream_checks``: a step that cannot run is *skipped with its reason*, never passed, and nothing
pretends an FBX exists:

* no pool, a fake pool, a worker that does not have the operation, or a result marked fake (or not ``ok``): skipped;
* a worker error (the comparison with the sources failed, a texture the FBX would have to reference is missing, the worker
  died): ``"failed"`` with a manifest-ready record of why. A failed export leaves no file behind (see
  ``fbx_export.export_fbx``). The FBX is an extra output, so the caller records the failure plainly and ships the package
  without it: a failed FBX never refuses a package and never changes whether it is verified or a preview.

What the FBX is, and what has not been verified, is in the record this returns (``kernel.package.fbx_record``): the
file was re-imported in Blender and compared with the package's GLB files; it was not opened in Unity, Unreal Engine or
Roblox Studio, none of which runs here.

Whoever calls this must guarantee that nothing else writes into the package directory while it runs. The export writes
under a temporary name that no other step uses and removes only paths carrying that name
(``fbx_export.written_by_export``), but a file another step is still writing would be compared and hashed in a
half-written state. Pass C therefore calls it last, after every step it started has finished (see
``tactics/pyramid.py``); the native export works in a private temporary directory, one step after another.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from antigravity3d.adapters.blender_5_3 import upstream_checks
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError
from antigravity3d.kernel.package import fbx_contradictions, fbx_failed_record, fbx_inputs, fbx_record, record_fbx

SEEN_ENGINES = "unity_urp, unreal_5, roblox_r15"


def eligible(manifest: dict[str, Any]) -> str | None:
    """None when this package gets an FBX; else why not (a reason for a skipped step)."""
    if fbx_inputs(manifest, Path(".")) is None:
        return f"no FBX for {manifest.get('target_engine')}: only {SEEN_ENGINES} get one (Godot imports the glTF)"
    return None


async def export_package_fbx(pool: Any, out_dir: Path, manifest: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Write ``<asset>.fbx`` into ``out_dir``: ``("passed", detail, worker result)``, ``("skipped", reason, {})`` or
    ``("failed", detail, fbx_failed_record)``; never raises (a cancelled task still cancels).

    ``manifest`` is the package's manifest data (``PackageManifest.data``): it names the GLB, LOD and collider files and
    the maps. The caller puts a passed result into the manifest with :func:`record`, a failed one with its ``fbx`` entry."""
    if why := eligible(manifest):
        return "skipped", why, {}
    if reason := upstream_checks.unavailable_reason(pool):
        return "skipped", reason, {}
    inputs = fbx_inputs(manifest, out_dir)
    assert inputs is not None
    try:
        result = await pool.call("export_fbx", **inputs)
        if result.get("fake") or not result.get("ok"):
            return "skipped", "the Blender worker is a fake (its result is marked fake): no FBX was written", {}
        if contradictions := fbx_contradictions(manifest, out_dir, result):
            Path(result["fbx"]["path"]).unlink(missing_ok=True)  # the file this call wrote: it must not ship
            raise ValueError("FBX contradicts the manifest: " + "; ".join(contradictions))
        text = detail(result)  # also proves the answer has the shape of an FBX result before anyone records it
        fbx_record(result)
    except Exception as e:  # noqa: BLE001 - a failed output is a result, recorded; the caller ships the package without it
        if isinstance(e, BlenderOpError) and ("unknown op" in e.msg or "is not loaded in this worker" in e.msg):
            return "skipped", f"the Blender worker cannot export FBX: {e.msg[:200]}", {}
        cls, msg = getattr(e, "cls", type(e).__name__), getattr(e, "msg", str(e))
        return "failed", f"{cls}: {msg}"[:500], fbx_failed_record(manifest, cls, msg)
    return "passed", text, result


def detail(result: dict[str, Any]) -> str:
    """One line for the run report: a fact about the file, never a claim about an engine."""
    rec = fbx_record(result)
    meshes = rec["contents"]["render_meshes"]
    hulls = rec["contents"]["collision_hulls"]
    omitted = "; left out: " + ", ".join(o["what"] for o in rec["contents"]["omitted"]) if rec["contents"]["omitted"] else ""
    lost = rec["maps"]["lost"]
    return (f"{rec['file']} by {upstream_checks.source('export_asset')}, preset {rec['preset']}: "
            f"{len(meshes)} mesh(es), {len(hulls)} collision hull(s), textures {rec['textures']['mode'].replace('_', ' ')}; re-imported in Blender and equal to the "
            f"package GLB(s) ({rec['comparison']['checks']} checks); maps the FBX does not carry: {', '.join(lost) if lost else 'none'}{omitted}; "
            f"not opened in Unity, Unreal Engine or Roblox Studio")[:700]


def record(manifest: dict[str, Any], out_dir: Path, result: dict[str, Any]) -> None:
    """Put the FBX into the manifest (``files``, ``file_hashes``, ``fbx``): call it before the manifest is written."""
    record_fbx(manifest, out_dir, result)
