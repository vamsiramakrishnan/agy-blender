"""Host side of the independent checks the pipelines run with EL4CTEO/blender-skills' scripts (``qa_check``, ``kit_check``).

The worker op is ``inspect_usd`` (see ``daemon.py``); this module only decides when a call may be made and how its
answer reads, so the prop pipeline (pass C) and the kit DAG say the same thing:

* A check that cannot run is *skipped with its reason*, never passed: no pool, a fake pool, a worker (also one
  reached through a socket, which has no ``fake`` attribute) whose answer is marked ``"fake": true``, or a worker
  that does not have the operation or the reviewed scripts.
* Topology checks run on the file with coincident vertices merged (``weld=True``): the package's GLB and USD split
  vertices at UV seams and hard edges, and read unmerged ``qa_check`` misses floating and inverted parts and
  reports duplicate vertices and open boundaries that are not defects (``benchmarks/README.md``, "Upstream checks survey").
* The merge is made part by part (``daemon.weld_within_parts``). A GLB that batches the parts of a material into one
  mesh puts separate closed parts in one object, and parts in contact have vertices at one position (a lid on a body
  of the same footprint). Merged across parts they read as one surface whose faces are wound against each other: the
  55-part crate of 2026-11-21 failed the Roblox profile on "16 edges with inconsistent face winding" with every part
  closed and consistently wound. What stays is the positions two parts share, which upstream reports as duplicate
  vertices (a warning); :func:`read_duplicates` says which of those are parts in contact.
"""

from __future__ import annotations

import re
from typing import Any

from antigravity3d.adapters.blender_5_3 import skills
from antigravity3d.adapters.blender_5_3.pool import BlenderOpError
from antigravity3d.kernel.fast_ops import TOUCH_TOL

# qa_check's touch / ground tolerance (its ``tol``), in metres: the kernel's own constant for the fast check
# (kernel/fast_ops tier1_qa), so the late check is never stricter than the fast one for the same defect and nothing
# that passes the fast check fails in Blender. Upstream's own default is the same 2 mm.
QA_TOL_M = TOUCH_TOL
FLOATING_PREFIX = "floating/detached part in "
DUPLICATES = re.compile(r"(?P<object>.+): duplicate verts=(?P<n>\d+) loose verts=0")
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


def read_duplicates(result: dict[str, Any]) -> dict[str, int]:
    """{upstream warning: vertices} for each "duplicate verts" warning that is parts in contact and nothing else.

    qa_check counts the vertices that have another within 0.01 mm, halved. After the merge within parts no part has two
    vertices at one position, and the worker reports how many vertices share a position with a vertex of another part
    (``agy_inspect.weld.objects[..].coincident_between_parts``). When that accounts for the whole count the warning is
    two parts touching at a corner, not vertices somebody forgot to merge. The upstream result is not altered."""
    objects = ((result.get("agy_inspect") or {}).get("weld") or {}).get("objects") or {}
    read: dict[str, int] = {}
    for w in result.get("warnings") or []:
        m = DUPLICATES.fullmatch(w)
        apart = int((objects.get(m["object"]) or {}).get("coincident_between_parts") or 0) if m else 0
        if m and apart and int(m["n"]) == apart // 2:
            read[w] = apart
    return read


def qa_verdict(result: dict[str, Any], profile: str) -> tuple[str, str]:
    """("failed" | "passed", detail) for a ``qa_check`` result. The detail is a fact about the file (no timings).

    Upstream's errors are quoted as it wrote them, and any error fails the check: the gate reads the script as the
    scorecard's independent check does (benchmarks/compare_eval.py: pass means no error). A "floating" piece whose lowest
    point is on the ground used to be read as a separate piece resting there and passed (commit 23c800d: "two crates side
    by side on the floor"); live 2026-11-22 (r5_crate_C) that let through a skid nothing touched, which the same script
    run on the delivered GLB failed. The detail still says which of the errors are pieces on the ground."""
    errors, resting, _ = read_floating(result)
    standing = errors
    contact = read_duplicates(result)
    warnings = [w + (f" (all {contact[w]} are vertices of separate parts in contact, not unmerged duplicates)" if w in contact else "")
                for w in result.get("warnings") or []]
    objects = result.get("objects") or {}
    tris = sum(int(o.get("tris", 0)) for o in objects.values())
    head = f"{source('qa_check')}, {profile} profile, vertices merged part by part: {len(objects)} object(s), {tris} tris"
    if standing:
        on_ground = (f" ({len(resting)} of them a piece whose lowest point is on the ground, touching no other part)"
                     if resting else "")
        return "failed", (head + f"; {len(standing)} error(s){on_ground}: " + "; ".join(standing))[:MAX_DETAIL]
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
    weld = ((r.get("agy_inspect") or {}).get("weld") or {}).get("objects") or {}
    return status, detail, {"script": "qa_check", "repository": skills.UPSTREAM_REPOSITORY, "commit": skills.UPSTREAM_COMMIT,
                            "profile": profile, "merged": True, "merged_within": "parts", "tol_m": QA_TOL_M, "errors": errors,
                            "resting_on_ground": resting, "warnings": list(r.get("warnings") or []),
                            "parts_in_contact_share_vertices": {k: v for k, v in sorted(read_duplicates(r).items())},
                            "pieces_after_merge": {k: int(v.get("parts") or 0) for k, v in sorted(weld.items())}}


def kit_verdict(results: dict[str, dict[str, Any]]) -> tuple[bool, dict[str, Any]]:
    """(all pieces pass, {piece file name: {"pass", "errors", "warnings"}}) for per-piece ``kit_check`` results."""
    summary: dict[str, Any] = {}
    for name, r in results.items():
        errors = [f"{obj}: {e}" for obj, rec in (r.get("pieces") or {}).items() for e in rec.get("errors", [])] + list(r.get("errors") or [])
        warnings = [f"{obj}: {w}" for obj, rec in (r.get("pieces") or {}).items() for w in rec.get("warnings", [])]
        summary[name] = {"pass": bool(r.get("pass")) and not errors, "errors": errors, "warnings": warnings}
    return all(v["pass"] for v in summary.values()), summary
