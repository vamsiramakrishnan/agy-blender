"""The look round: somebody looks at the colours before the asset ships.

The author chooses every material (colour, roughness, metalness, surface words) in its first answer and never sees
them: pass A's renders are clay, and pass B looks only when image textures exist. Measured 2026-11-22: the crate of
"a rugged military supply crate ..." shipped pale sage green (paint (0.18, 0.22, 0.14) linear, where an olive drab is
about 0.05), and a blind judge's reason for preferring a stock agent's crate was its "richer olive drab coloring,
superior metallic material separation on the latches".

One round (``SpendPlan.look_rounds``), after the design is final and its export bundle exists, before the package is
written (``pyramid._Run.pass_c``):

1. the package is written as it would ship (``write_engine_package``, straight into the package directory) and
   rendered in colour by the browser engine the pipeline already verifies with (three.js, ``web_three``), from two
   opposite corners (``LOOK_VIEWS``: every side and the top);
2. the judge answers the "look" stage's questions about those renders (``decisions.questions``: the surface defects,
   ``pbr_realism``, ``material_separation`` and ``score_brief_look``, how the colours and materials match the request);
3. the author is shown the renders, the request, its materials and the judge's answers and returns material operations
   only, or none (``MeteredAuthor.look``, ledger ``author.look``; an answer that does not read is corrected once);
4. the changed package is written beside it and rendered, the judge reviews it again and, on the generative fallback,
   compares before against after in both orders; the change is kept only when the after state is closer to the request
   in both orders and no question is newly answered yes (``decide``). Measured live on 2026-11-22: a change a person
   calls plainly better moved the separate 0-4 look scores by 0.02, while the pairwise judge preferred it 4 of 4. A
   calibrated judge keeps the absolute rule (``keep_look_change``). A kept change's package becomes the package; an undone one is
   discarded and the first stays. Pass C then verifies what is there: no state is written twice.

The look round never changes geometry, alpha or image maps (``ir_schema.apply_look``), so the export bundle stays
valid whatever is kept. Without a brief, an author that has no look request, a browser renderer or room in the budget
it is skipped with that reason.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from PIL import Image

from antigravity3d.ir import IRPatch, SceneIR
from antigravity3d.kernel import smart_source
from antigravity3d.tactics.common import png_bytes

# The two views, glTF Y-up camera directions from the asset's centre: the verification's own (``web_three.VIEW_DIR_YUP``,
# front, right side and top) and the opposite corner (back, left side and top). Together every side face and the top;
# the bottom is not seen. Measured 2026-11-22 on the crate package of r5/live/prop_crate: 2.9 s for the first view (the
# browser starts), 1.4 s for the next.
LOOK_VIEWS: tuple[tuple[str, tuple[float, float, float]], ...] = (
    ("front right", (1.4, 1.2, 2.0)),
    ("back left", (-1.4, 1.2, -2.0)),
)
# The renders are transparent where there is no asset; the judge and the author see them on a neutral mid grey (18 %
# reflectance, sRGB 118), against which a dark metal and a pale paint both read.
LOOK_BACKGROUND = (118, 118, 118)
LOOK_STAGE = "look"
# PROVISIONAL. The band a look score must move by before a change is kept or undone on it, where the absolute rule
# decides (a calibrated judge, ``keep_look_change``): the design loop's band (pyramid.IMPROVE_NOISE_BAND, measured for
# the shape questions). Live on 2026-11-22 the fallback's look scores spread 0.29 (score_brief_look 1.94-2.23, three
# repeats) and a clearly better change moved their mean by 0.02, so on the fallback the pairwise rule decides instead.
LOOK_NOISE_BAND = 0.20
# Planning only (admission reserves each call's counted worst case): one look request, the rules text, the material
# table and two images in (about 10,000 tokens, as a propose request's text) and a few thousand out. PROVISIONAL until
# the live check measures it; token_plan.LOOK_ROUND_TOKENS_EST is built from the same figure.
LOOK_AUTHOR_TOKENS_EST = 16_000

LOOK_HEAD = ("Look round. The design is final and has passed its checks; this round is about its materials only. The "
             "images are renders of the asset exactly as it ships (its game-engine package in a browser engine, lit by two "
             "lights and a sky, on a neutral grey background), from the front right and from the back left.")
LOOK_ASK = (
    "The goal is the request's colour and material words, and what a real one is made of where the request says nothing. "
    "Compare the renders with them:\n"
    "- does each material read as what it should be (a colour the request names is that colour, not a pale, washed-out or "
    "greyed version of it; paint reads as paint, bare or worn metal as metal, rubber as rubber, wood as wood);\n"
    "- do parts of different materials stand apart where a real one's do (a latch or a handle against the panel it is "
    "mounted on, a frame against its panel);\n"
    "- do roughness and metalness fit each material (bare metal is metallic about 1; paint, wood and rubber 0; rubber and "
    "raw wood are rough, worn metal and gloss paint less so).\n"
    "Colours are linear RGB 0-1, as the table lists them; the renders show them after the engine's sRGB conversion and "
    "lighting, so a linear value looks much lighter than its number (linear 0.18 shows as sRGB 0.46).\n"
    "Return one JSON object {\"ops\": [...]}: upsert_material with a material's id and only the fields you change, or "
    "assign_material to move a part to another existing material. Return {\"ops\": []} if every material already reads "
    "right. Do not add materials, and do not change alpha, opacity, transmission or image maps. The judge's assessment "
    "below is evidence, not the goal: a change is kept only if it brings the materials closer to the request, never "
    "because a score went up.")


@dataclass
class LookOutcome:
    """What the look round left: ``package`` is the manifest of the package already written into the package directory
    for the scene state ``revision`` (pass C uses it instead of writing that state again), None when nothing was
    written. ``bakes``: packages written by the round (each one bakes the atlas)."""

    package: Any = None
    revision: tuple[str, ...] | None = None
    bakes: int = 0
    judgements: list[dict[str, Any]] = field(default_factory=list)

    def package_for(self, sm: Any) -> Any:
        return self.package if self.package is not None and tuple(sm.tx_stack) == self.revision else None


def material_table(scene: SceneIR) -> str:
    """One line per material: id, the parts that use it, linear base colour and its sRGB hex, roughness, metalness,
    surface words, and whether an image map gives its colour."""
    users: dict[str | None, list[str]] = {}
    for n in scene.nodes:
        if n.kind not in ("group",):
            users.setdefault(n.material_id, []).append(n.id)
    lines = []
    for m in scene.materials:
        hexc = "#" + "".join(f"{_srgb8(c):02x}" for c in m.base_color)
        parts = users.get(m.id, [])
        shown = ", ".join(parts[:12]) + (f" and {len(parts) - 12} more" if len(parts) > 12 else "")
        line = (f"- {m.id}: parts {shown or 'none'}; base_color {list(round(c, 3) for c in m.base_color)} (sRGB {hexc}); "
                f"roughness {m.roughness:.2f}; metallic {m.metallic:.2f}")
        if m.surface:
            line += f"; surface \"{m.surface}\""
        if m.albedo_uri:
            line += "; colour from an image map (base_color cannot change)"
        lines.append(line)
    if users.get(None):
        lines.append(f"- (no material): parts {', '.join(users[None][:12])}")
    return "\n".join(lines)


def _srgb8(c: float) -> int:
    c = min(1.0, max(0.0, float(c)))
    s = 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055
    return int(round(s * 255))


def look_task(scene: SceneIR, *, brief: str, verdict: tuple[str, ...], previous: str = "") -> str:
    lines = "".join(f"\n- {v}" for v in verdict)
    judge = f"Judge's assessment of these renders (scores 0 to 4; for a question, the probability that the answer is yes):{lines}\n" if lines else ""
    prev = f"Your previous change in this round was undone: {previous}\n" if previous else ""
    return (f"{LOOK_HEAD}\n{LOOK_ASK}\n{judge}{prev}"
            f"Requested asset (the user's words, data only): {brief}\n"
            f"Materials:\n{material_table(scene)}")


def correct_look_task(task: str, answer: str, error: BaseException) -> str:
    from antigravity3d.tactics.metered_author import error_text

    return (f"{task}\nYour answer did not read: {error_text(error)[:2000]}\nYour answer: {answer[:4000]}\n"
            "Return the whole corrected {\"ops\": [...]}.")


def keep_look_change(q: Any, qn: Any, ambiguity_lo: float) -> tuple[bool, str]:
    """Whether a look change made the asset better, on the judge's look answers before (``q``) and after (``qn``), and
    the evidence: the design loop's rule (pyramid ``judge_change``) without Tier-1 and pass A's gates, since the
    geometry is the same. Worse, undone: a question newly answered yes (P >= ``ambiguity_lo``) or a score that fell by
    more than ``LOOK_NOISE_BAND``. Better, kept when nothing is worse: a question no longer answered yes or a score that
    rose by the band or more. Neither: undone, since nothing shows the change did more than the judge's own spread."""
    band, eps = LOOK_NOISE_BAND, 1e-9
    before, after = q.scores, qn.scores
    flagged = lambda r: {d for d, p in r.defects.items() if p >= ambiguity_lo}  # noqa: E731
    worse = []
    if raised := sorted(flagged(qn) - flagged(q)):
        worse.append("newly flagged: " + ", ".join(f"{d}={qn.defects[d]:.2f}" for d in raised))
    if fell := [f"{k} {v:.2f}->{after[k]:.2f}" for k, v in sorted(before.items()) if k in after and after[k] < v - band - eps]:
        worse.append(f"fell by more than the band ({band}): " + ", ".join(fell))
    if worse:
        return False, "; ".join(worse)
    better = []
    if cleared := sorted(flagged(q) - flagged(qn)):
        better.append("cleared " + ", ".join(f"{d} {q.defects[d]:.2f}->{qn.defects.get(d, 0.0):.2f}" for d in cleared))
    if rose := [f"{k} {v:.2f}->{after[k]:.2f}" for k, v in sorted(before.items()) if k in after and after[k] >= v + band - eps]:
        better.append(f"rose by the band ({band}) or more: " + ", ".join(rose))
    if not better:
        return False, f"no change beyond the band ({band}) and no question cleared"
    return True, "; ".join(better)


def compares(plane: Any) -> bool:
    """Whether the look round decides on a pairwise comparison: the generative fallback answers one; a calibrated judge
    (the native Decisions API) keeps the absolute rule, ``keep_look_change``, since no pairwise request is made for a
    service this project has not been able to call."""
    return not getattr(plane, "calibrated", True) and hasattr(plane, "compare_states")


def keep_by_pairs(p1: Any, p2: Any) -> tuple[bool, str]:
    """Kept only when the after state is closer to the request in both orders (``p1``: before shown as state A, ``p2``:
    after shown as state A). A split or "neither" is not shown to be better: undone. Both reasons are the evidence."""
    win = p1.closer == "B" and p2.closer == "A"
    said = (f"before-first: {_said(p1, after='B')}; after-first: {_said(p2, after='A')}")
    return win, (f"{KEPT}: " if win else f"{UNDONE_NOT_BETTER}: ") + said


def _said(p: Any, *, after: str) -> str:
    who = "after" if p.closer == after else "neither" if p.closer == "neither" else "before"
    return f"{who} ({p.probabilities.get(p.closer, 0.0):.2f}) \"{p.reason}\""


# The three ways a look decision on the generative fallback goes, each the head of its evidence in the step.
KEPT = "kept (preferred in both orders)"
UNDONE_NOT_BETTER = "undone (not shown to be better)"
UNDONE_DEFECT = "undone (new defect confirmed)"


def decide(q: Any, qn: Any, pairs: Any, ambiguity_lo: float, qn2: Any = None) -> tuple[bool, str]:
    """Keep or undo a look change. With pairwise judgements (``compares``) ``keep_by_pairs`` decides, and the separate
    look scores are reported, not used. A question the after state's review newly answers yes (P >= ``ambiguity_lo``,
    not so on the before state) by either after-review is confirmed when the other (``qn2``) answers yes too: then it undoes the
    change even when the after state is preferred (a darker crate that has turned to the wrong material is not better);
    unconfirmed, it is recorded and decides nothing. Live on 2026-11-22 one review's defect_wrong_material of 0.55 would
    have undone a change the same judge preferred six times out of six. Without pairwise judgements:
    ``keep_look_change``."""
    if pairs is None:
        return keep_look_change(q, qn, ambiguity_lo)
    flagged = lambda r: {d for d, p in r.defects.items() if p >= ambiguity_lo}  # noqa: E731
    after = flagged(qn) | (flagged(qn2) if qn2 is not None else set())
    raised = sorted(after - flagged(q))  # newly answered yes by either after-review (they run together, in no order)
    confirmed = [d for d in raised if qn2 is not None and d in flagged(qn) and d in flagged(qn2)]
    kept, said = keep_by_pairs(*pairs)
    flags = lambda ds: ", ".join(f"{d}={qn.defects.get(d, 0.0):.2f}/{(qn2.defects.get(d, 0.0) if qn2 is not None else float('nan')):.2f}" for d in ds)  # noqa: E731
    if confirmed:
        return False, f"{UNDONE_DEFECT}: {flags(confirmed)} (after reviews; before {', '.join(f'{q.defects.get(d, 0.0):.2f}' for d in confirmed)}); pairwise: {said.split(': ', 1)[1]}" \
            + ("" if kept else " (not preferred either)")
    note = f"; unconfirmed new flag(s), decided nothing: {flags(raised)}" if raised else ""
    return kept, said + note


def renderer_unavailable() -> str | None:
    from antigravity3d.adapters.engines.web_three import WebThreeValidator

    v = WebThreeValidator()
    return None if v.available() else (v._unavailable() or "three.js web renderer unavailable")


async def render_package(pkg: Path, out_dir: Path) -> list[tuple[str, bytes]]:
    """The package's main GLB rendered by three.js from ``LOOK_VIEWS``, each composited on ``LOOK_BACKGROUND``."""
    from antigravity3d.adapters.engines import web_three as W

    manifest = json.loads((pkg / "manifest.json").read_text())
    glb = next(p for p in sorted(pkg.glob("*.glb")) if "_LOD" not in p.name and "collider" not in p.name)
    data = glb.read_bytes()
    files = W.referenced_textures(pkg, [data])
    exe = W.WebThreeValidator().binary()
    out = []
    for name, view_dir in LOOK_VIEWS:
        page_dir = out_dir / name.replace(" ", "_")
        await asyncio.to_thread(W.build_page, page_dir, data, manifest["glb_bounds_m"], files=files, view_dir=view_dir)
        r = await W.run_page(page_dir, exe, W.DEFAULT_TIMEOUT)
        raw = r["raw"]
        if raw.get("errors") or not raw.get("png"):
            raise RuntimeError(f"three.js rendered no frame for the {name} view: {(raw.get('errors') or ['no image'])[0]}"[:300])
        out.append((name, _on_grey(base64.b64decode(raw["png"].split(",", 1)[1]))))
        shutil.rmtree(page_dir, ignore_errors=True)
    return out


def _on_grey(png: bytes) -> bytes:
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    bg = Image.new("RGBA", im.size, (*LOOK_BACKGROUND, 255))
    return png_bytes(Image.alpha_composite(bg, im).convert("RGB"))


class LookRound:
    """One look round over a ``pyramid._Run`` (its stage, judge, author, governor, budget and workdir)."""

    def __init__(self, run: Any, bundle: Any, pkg: Path, write: Any) -> None:
        self.run, self.bundle, self.pkg, self.write = run, bundle, pkg, write
        self.dir = run.workdir / "look"
        self.outcome = LookOutcome()
        self.placed: list[str] = []

    async def package(self, out: Path) -> Any:
        """Write the current state as a package into ``out`` (``write``: pass C's own writer, the same arguments)."""
        m = await asyncio.to_thread(self.write, out)
        self.outcome.bakes += 1
        # the stencil markings a generated surface painted on purpose (kernel.smart_source): the look review is told
        # them, so its question about logos and text asks about any OTHER text (decisions.engine.evaluate_visual_qa)
        self.placed = smart_source.placed_strings(json.loads((Path(out) / "manifest.json").read_text()))
        return m

    async def judge(self, views: list[tuple[str, bytes]]) -> Any:
        run = self.run
        uris = await asyncio.gather(*(run.upload(png) for _, png in views))
        shown = tuple((f"{name} view", uri) for (name, _), uri in zip(views, uris))
        r = await run.plane.evaluate_visual_qa(shown, run.ortho_uri, run.style_uri, LOOK_STAGE, governor=run.governor,
                                               brief=run.prompt, placed_markings=self.placed)
        run.res.cost_usd += r.result.booked_usd
        return r, uris

    async def judge_after(self, views: list[tuple[str, bytes]], uris: list[str], views_n: list[tuple[str, bytes]]) -> Any:
        """The after state's look review and, on the generative fallback, a second review (which confirms or not a newly
        flagged question, ``decide``) and the two pairwise comparisons of before against after (both orders), all in
        flight together: ((verdict, uris), (pair before-as-A, pair after-as-A)) or (..., None) on a judge that compares
        nothing (``compares``)."""
        run = self.run
        if not compares(run.plane):
            return await self.judge(views_n), None
        uris_n = list(await asyncio.gather(*(run.upload(png) for _, png in views_n)))
        names = [f"{name} view" for name, _ in views]
        before, after = tuple(zip(names, uris)), tuple(zip(names, uris_n))

        async def review() -> Any:
            r = await run.plane.evaluate_visual_qa(after, run.ortho_uri, run.style_uri, LOOK_STAGE, governor=run.governor, brief=run.prompt,
                                                   placed_markings=self.placed)
            run.res.cost_usd += r.result.booked_usd
            return r

        async def pair(a: Any, b: Any) -> Any:
            p = await run.plane.compare_states(a, b, aspect="look", brief=run.prompt, governor=run.governor)
            run.res.cost_usd += p.result.booked_usd
            return p

        qn, qn2, p1, p2 = await asyncio.gather(review(), review(), pair(before, after), pair(after, before))
        self.second_review = qn2
        return (qn, uris_n), (p1, p2)

    def keep(self, path: Path, views: list[tuple[str, bytes]], tag: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        for name, png in views:
            (path / f"{tag}_{name.replace(' ', '_')}.png").write_bytes(png)

    async def run_round(self) -> LookOutcome:
        """``SpendPlan.look_rounds`` rounds; a round after one whose change was undone, or that changed nothing, would
        ask the same request about the same renders, so the rounds end there."""
        run, out = self.run, self.outcome
        t0 = time.perf_counter()
        why = self.skip_reason()
        if why:
            run.step("look_round", "skipped", why, t0)
            return out
        sm = run.sm
        try:
            out.package = await self.package(self.pkg)
            out.revision = tuple(sm.tx_stack)
            views = await render_package(self.pkg, self.dir / "pages")
            self.keep(self.dir, views, "round1_before")
            q, uris = await self.judge(views)
        except Exception as e:  # noqa: BLE001 - the round is a refinement: without a render or a verdict the asset ships as it is
            run.step("look_round", "skipped", f"look round not judged: {type(e).__name__}: {e}"[:400], t0)
            return out
        out.judgements.append({"round": 1, "state": "before", "scores": dict(q.scores), "defects": dict(q.defects)})
        for k in range(1, run.governor.plan.look_rounds + 1):
            step = await self.one(k, views, uris, q, t0)
            if step is None:
                break
            views, uris, q = step
            t0 = time.perf_counter()
        shutil.rmtree(self.dir / "pages", ignore_errors=True)
        run.res.objects["look_round"] = {"judgements": out.judgements, "bakes": out.bakes, "renders": str(self.dir)}
        return out

    async def one(self, k: int, views: list[tuple[str, bytes]], uris: list[str], q: Any, t0: float) -> Any:
        """Round ``k`` from the package in the package directory, its renders and verdict: (the new ones) when its change
        was kept, None when the rounds end."""
        from antigravity3d.tactics.pyramid import verdict_lines

        run, out, sm = self.run, self.outcome, self.run.sm
        name = "look_round" if k == 1 else f"look_round_{k}"
        scene = sm.base_ir()
        task = look_task(scene, brief=run.prompt, verdict=verdict_lines(q, run.th))
        images = tuple((f"{v} render of the asset as it ships (Image {i})", uri) for i, ((v, _), uri) in enumerate(zip(views, uris), 1))
        try:
            patch = await run.author.look(scene, task, images=images,
                                          correct=lambda answer, err: correct_look_task(task, answer, err))
        except Exception as e:  # noqa: BLE001 - an author call that failed changes nothing
            run.step(name, "warned", f"author look request failed, materials unchanged: {type(e).__name__}: {e}"[:400], t0)
            return None
        if patch is None:
            run.step(name, "passed", self.detail("the author kept every material", q, None), t0)
            return None
        tid = sm.apply_patch(patch)
        changed = _summary(patch)
        after_dir = self.dir / f"round{k}_package"
        try:
            m_after = await self.package(after_dir)
            views_n = await render_package(after_dir, self.dir / "pages")
            self.keep(self.dir, views_n, f"round{k}_after")
            (qn, uris_n), pairs = await self.judge_after(views, uris, views_n)
        except Exception as e:  # noqa: BLE001
            sm.rollback(tid, discard=True)
            shutil.rmtree(after_dir, ignore_errors=True)
            run.step(name, "warned", f"change {changed} undone, not judged: {type(e).__name__}: {e}"[:400], t0)
            return None
        qn2 = getattr(self, "second_review", None) if pairs is not None else None
        kept, evidence = decide(q, qn, pairs, run.th.ambiguity_lo, qn2)
        out.judgements.append({"round": k, "state": "after", "change": changed, "kept": kept, "evidence": evidence,
                               "scores": dict(qn.scores), "defects": dict(qn.defects),
                               **({"second_review": {"scores": dict(qn2.scores), "defects": dict(qn2.defects)}} if qn2 is not None else {}),
                               **({"pairs": [{"order": o, "closer": p.closer, "probabilities": p.probabilities, "reason": p.reason}
                                             for o, p in zip(("before as A", "after as A"), pairs)]} if pairs else {})})
        if not kept:
            sm.rollback(tid, discard=True)
            shutil.rmtree(after_dir, ignore_errors=True)
            run.step(name, "warned", self.detail(f"undone {changed}: {evidence}", q, qn), t0)
            return None
        _replace_package(after_dir, self.pkg)
        out.package, out.revision = replace(m_after, out_dir=self.pkg), tuple(sm.tx_stack)
        run.step(name, "passed", self.detail(f"kept {changed}: {evidence}", q, qn), t0)
        return views_n, uris_n, qn

    def detail(self, head: str, q: Any, qn: Any) -> str:
        def fig(r: Any) -> str:
            return ", ".join(f"{k}={v:.2f}" for k, v in sorted(r.scores.items()))

        tail = f"; before: {fig(q)}" + (f"; after: {fig(qn)}" if qn is not None else "")
        return (head + tail + f"; {self.outcome.bakes} bake(s)")[:600]

    def skip_reason(self) -> str | None:
        run = self.run
        if not run.prompt:
            return "no request to compare the materials with"
        if getattr(run.author, "look", None) is None:
            return "the author has no look request (offline template author)"
        if not run.sm.base_ir().materials:
            return "the asset has no materials"
        if (why := renderer_unavailable()) is not None:
            return f"no colour renderer: {why}"
        from antigravity3d.tactics.pyramid import QA_EST_USD, QA_TOKENS_EST

        judged = 5 if compares(run.plane) else 2  # before, after twice and the two pairwise orders on the fallback
        need = run.governor.est(usd=judged * QA_EST_USD, tokens=judged * QA_TOKENS_EST + LOOK_AUTHOR_TOKENS_EST)
        if run.governor.remaining < need:
            return f"budget: {run.governor.fmt(run.governor.remaining)} left, a look round needs about {run.governor.fmt(need)}"
        return None


def _summary(patch: IRPatch) -> str:
    out = []
    for op in patch.ops:
        if op.op == "upsert_material":
            m = op.material
            out.append(f"{m.id} base_color {[round(c, 3) for c in m.base_color]} roughness {m.roughness:.2f} metallic {m.metallic:.2f}")
        else:
            out.append(f"{op.node_id} -> {op.material_id}")
    return "; ".join(out)


def _replace_package(src: Path, dst: Path) -> None:
    """Move the package written in ``src`` into ``dst`` (whose previous files go). The package is a function of the scene
    and the budget, not of its directory (``write_engine_package``), so the moved files are the files a write into
    ``dst`` would make."""
    for p in dst.iterdir():
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    for p in src.iterdir():
        shutil.move(str(p), str(dst / p.name))
    shutil.rmtree(src, ignore_errors=True)


async def look_round(run: Any, bundle: Any, pkg: Path, write: Any) -> LookOutcome | None:
    """The look rounds of ``run``'s plan (``SpendPlan.look_rounds``); None and nothing recorded when the plan has none."""
    if getattr(run.governor.plan, "look_rounds", 0) <= 0:
        return None
    if not getattr(run, "new_design", True):
        # An edit of a shipped asset (refine(run_pass_a=False), dags.edit_asset) is not looked at: its verdict may still be
        # arriving when pass C starts and goes into the package, so a package written here could be stale, and the edit
        # command says what it did not review. Nothing is recorded: an edit's run is what it was.
        return None
    return await LookRound(run, bundle, pkg, write).run_round()


__all__ = ["LOOK_NOISE_BAND", "LOOK_VIEWS", "LookOutcome", "keep_look_change", "look_round", "look_task", "material_table",
           "render_package"]
