"""Host side of the independent checks the pipelines run with EL4CTEO/blender-skills' scripts (``qa_check``, ``kit_check``).

The worker op is ``inspect_usd`` (see ``daemon.py``); this module only decides when a call may be made and how its
answer reads, so the prop pipeline (pass C) and the kit DAG say the same thing:

* A check that cannot run is *skipped with its reason*, never passed: no pool, a fake pool, a worker (also one
  reached through a socket, which has no ``fake`` attribute) whose answer is marked ``"fake": true``, or a worker
  that does not have the operation or the reviewed scripts.
* Topology checks run on the file with coincident vertices merged (``weld=True``): the package's GLB and USD split
  vertices at UV seams and hard edges, and read unmerged ``qa_check`` misses floating and inverted parts and
  reports duplicate vertices and open boundaries that are not defects (``benchmarks/README.md``, "Upstream checks survey").
"""

from __future__ import annotations

from typing import Any

from antigravity3d.adapters.blender_5_3 import skills
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError
from antigravity3d.kernel.fast_ops import TOUCH_TOL

# qa_check's touch / ground tolerance (its ``tol``), in metres: the kernel's own constant for the fast check
# (kernel/fast_ops tier1_qa), so the late check is never stricter than the fast one for the same defect and nothing
# that passes the fast check fails in Blender. Upstream's own default is the same 2 mm.
QA_TOL_M = TOUCH_TOL
FLOATING_PREFIX = "floating/detached part in "
SHOWN_WARNINGS = 4
MAX_DETAIL = 600


class CheckUnavailable(Exception):
    """The check cannot run here; ``str(exc)`` is the reason recorded with the skipped step."""


def source(script: str) -> str:
    """Names the script, repository and pinned commit once, for a reader of a manifest who has never heard of either."""
    return f"{script} of {skills.UPSTREAM_REPOSITORY.removeprefix('https://github.com/')} @{skills.UPSTREAM_COMMIT[:7]} (release {skills.UPSTREAM_RELEASE})"


def qa_profile(target_engine: str) -> str:
    """``qa_check`` profile for an engine: Roblox's rules (closed render meshes, 20000 triangles) or ``game``."""
    return "roblox" if str(target_engine).startswith("roblox") else "game"


def unavailable_reason(pool: Any) -> str | None:
    if pool is None:
        return "no Blender pool attached"
    if getattr(pool, "fake", False):
        return "the Blender pool is fake (no real Blender): a stand-in result is not a check"
    return None


async def run_primitive(pool: Any, path: str, primitive: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """One ``inspect_usd`` call, merged. Raises :class:`CheckUnavailable` when no real check came back."""
    if reason := unavailable_reason(pool):
        raise CheckUnavailable(reason)
    try:
        result = await pool.call("inspect_usd", path=path, primitive=primitive, kwargs=kwargs, weld=True)
    except BlenderOpError as e:
        if "unknown op" in e.msg or "is not loaded in this worker" in e.msg:
            raise CheckUnavailable(f"the Blender worker cannot run {primitive}: {e.msg[:200]}") from e
        raise
    if result.get("fake"):
        raise CheckUnavailable("the Blender worker is a fake (its result is marked fake): a stand-in result is not a check")
    return result


def read_floating(result: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """(errors upstream reported, the ones read as separate pieces resting on the ground, the ones that stand).

    qa_check reports every connected piece other than the biggest that touches nothing as floating, with no ground:
    two crates side by side, each on the floor, are "floating". The worker lists the connected pieces of the merged
    meshes beside the result (``agy_inspect.pieces``, with the lowest z of each); a reported piece whose lowest point
    is within ``QA_TOL_M`` of the ground plane (z = 0) is a piece resting on the ground. The upstream result is not
    altered: this reads it.
    """
    errors = list(result.get("errors") or [])
    floating = list(result.get("floating") or [])
    pieces = (result.get("agy_inspect") or {}).get("pieces") or []
    reported = [e for e in errors if e.startswith(FLOATING_PREFIX)]
    resting: list[str] = []
    if len(reported) == len(floating):  # one error per reported piece, in order; otherwise nothing is read
        for err, rec in zip(reported, floating):
            centre = list(rec.get("center") or [])
            match = [p for p in pieces if p["object"] == rec.get("object") and p["verts"] == rec.get("island_verts")
                     and len(centre) == 3 and all(abs(a - b) <= 1.5e-3 for a, b in zip(p["center"], centre))]
            if len(match) == 1 and abs(match[0]["min_z"]) <= QA_TOL_M:
                resting.append(err)
    standing = [e for e in errors if e not in resting]
    return errors, resting, standing


def qa_verdict(result: dict[str, Any], profile: str) -> tuple[str, str]:
    """("failed" | "passed", detail) for a ``qa_check`` result. The detail is a fact about the file (no timings).

    Upstream's errors are quoted as it wrote them. A "floating" piece that rests on the ground is read as a separate
    piece, not a defect, and the detail says both."""
    errors, resting, standing = read_floating(result)
    warnings = list(result.get("warnings") or [])
    objects = result.get("objects") or {}
    tris = sum(int(o.get("tris", 0)) for o in objects.values())
    head = f"{source('qa_check')}, {profile} profile, vertices merged: {len(objects)} object(s), {tris} tris"
    if standing:
        return "failed", (head + f"; {len(standing)} error(s): " + "; ".join(standing))[:MAX_DETAIL]
    shown = "; ".join(warnings[:SHOWN_WARNINGS]) + (f"; +{len(warnings) - SHOWN_WARNINGS} more" if len(warnings) > SHOWN_WARNINGS else "")
    note = ""
    if resting:
        note = (f"; upstream reported {len(resting)} detached piece(s) ({'; '.join(resting)}), "
                f"read here as separate pieces resting on the ground (lowest point within {QA_TOL_M * 1000:g} mm of z=0), not floating")
    return "passed", (head + (f"; no errors, {len(warnings)} warning(s)" if not resting else f"{note}; {len(warnings)} warning(s)")
                      + (f": {shown}" if warnings else ""))[:MAX_DETAIL]


async def asset_qa(pool: Any, glb: str, target_engine: str) -> tuple[str, str, dict[str, Any]]:
    """The one call both pipelines make: ``qa_check`` on the exported GLB, merged, with the target engine's profile.

    Returns ("passed" | "failed" | "skipped", detail, data for the manifest). The GLB is the file the engines import
    (the same bytes for every engine, no node transforms). A worker error propagates: the caller records it as a
    failure, as it does for the engine import.
    """
    profile = qa_profile(target_engine)
    try:
        r = await run_primitive(pool, glb, "qa_check", {"profile": profile, "tol": QA_TOL_M})
    except CheckUnavailable as e:
        return "skipped", str(e), {}
    status, detail = qa_verdict(r, profile)
    errors, resting, _ = read_floating(r)
    return status, detail, {"script": "qa_check", "repository": skills.UPSTREAM_REPOSITORY, "commit": skills.UPSTREAM_COMMIT,
                            "profile": profile, "merged": True, "tol_m": QA_TOL_M, "errors": errors, "resting_on_ground": resting,
                            "warnings": list(r.get("warnings") or [])}


def kit_verdict(results: dict[str, dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    """(all pieces pass, {piece file name: {"pass", "errors", "warnings"}}) for per-piece ``kit_check`` results."""
    summary: dict[str, Any] = {}
    for name, r in results.items():
        errors = [f"{obj}: {e}" for obj, rec in (r.get("pieces") or {}).items() for e in rec.get("errors", [])] + list(r.get("errors") or [])
        warnings = [f"{obj}: {w}" for obj, rec in (r.get("pieces") or {}).items() for w in rec.get("warnings", [])]
        summary[name] = {"pass": bool(r.get("pass")) and not errors, "errors": errors, "warnings": warnings}
    return all(v["pass"] for v in summary.values()), summary
