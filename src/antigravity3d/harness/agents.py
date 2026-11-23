"""Stage-scoped agent factories, the Turn-0 director, an external-completion trigger and run_stage."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import pydantic
from google.antigravity import Agent, types
from google.antigravity.connections.local.local_connection_config import (
    LocalAgentConfig,
)
from google.antigravity.hooks import policy
from google.antigravity.models import (
    GeminiAPIEndpoint,
    GeminiModelOptions,
    ModelTarget,
    ModelType,
)
from google.antigravity.triggers import TriggerContext

from antigravity3d.art_direction import author_text, texture_brief
from antigravity3d.authoring_contract import AuthoringIntent, stage_geometry_guidance
from antigravity3d.harness.hooks import (
    QAStopHook,
    TelemetryHook,
    VisionEvictionHook,
    _Shared,
    sanitize_prompt,
    stage_hooks,
    state_header,
    with_state_header,
)
from antigravity3d.harness.session import (
    CTX_REGISTRY_KEY,
    CTX_SESSION_KEY,
    DEFAULT_REGISTRY,
    bind_conversation,
    session,
    set_toolset,
    unbind_conversation,
)
from antigravity3d.harness.tools import (
    STAGE_SUBAGENTS,
    STAGE_TOOLS,
    SUBAGENT_TOOLS,
    TOOLSET_STAGE,
)
from antigravity3d.ir_schema import (  # prose the tool schemas cannot carry
    IR_SEMANTICS,
    OP_SEMANTICS,
    SHAPE_SEMANTICS,
)
from antigravity3d.models import Role, tier
from antigravity3d.state import SceneSessionRegistry

Toolset = Literal["BLOCKOUT", "TWEAK", "VARIANTS", "LOOKDEV", "EXPORT"]
DEFAULT_ROLE: dict[str, Role] = {
    "BLOCKOUT": "builder", "TWEAK": "bulk", "VARIANTS": "builder", "LOOKDEV": "builder", "EXPORT": "bulk", "PLAN": "director",
}
COMPACTION_TOKENS = 48_000
TOOL_OUTPUT_MAX_TOKENS = 1_500  # backstop; harness tools already return one-liners
# What a stage agent is, said with the SDK's own mechanisms (measured with tests/gemini_stub.py on the request the
# harness posts; see tests/test_stage_request.py):
# * CustomSystemInstructions: the system prompt is exactly STAGE_TEXT. A plain string (the old form) is appended to the
#   default coding-assistant template (identity, markdown/LaTeX/link style, transcript files, subagent and messaging
#   sections): about 7.6k characters per call that describe no asset work. TemplatedSystemInstructions(identity=...)
#   only swaps the 491-character identity and keeps the rest.
# * AgentBehavior.MINIMAL: the harness stops injecting the toolSummary/toolAction parameters into every declaration
#   (about 450-520 characters per tool, and two strings the model had to write on every call that nothing reads). It
#   changes nothing else in the request: contents, generation config and stop-hook continuations are identical.
# * Builtins: only FINISH. VIEW_FILE was enabled everywhere and used nowhere (no instruction refers to a file, the
#   tools return one-line deltas and URIs). INVOKE_SUBAGENT only where the stage has a subagent (tools.STAGE_SUBAGENTS).
STAGE_BUILTINS = [types.BuiltinTools.FINISH]
STAGE_BEHAVIOR = types.AgentBehavior.MINIMAL

# Only what a schema cannot say: units, axes, anchors, ground contact, id rules. Field names and types are in the
# tool declarations (generated from the same IR schema), so they are not repeated here.
_IR_RULES = f"{IR_SEMANTICS}\n{SHAPE_SEMANTICS}\n{OP_SEMANTICS}"
_IR_STAGES = frozenset({"BLOCKOUT", "TWEAK", "VARIANTS"})  # edit geometry: IR semantics and the anchor ops
_MATERIAL_STAGES = frozenset({"LOOKDEV"})  # edit materials only: IR semantics (ids, material cross-field rules)

_STAGE_TEXT: dict[str, str] = {
    "BLOCKOUT": "Use apply_scene_ir (batch ops; main solids before required detail). "
                "Each edit is atomic: a failing Tier-1 check (non-manifold, over budget) is rolled back and reported in one "
                "line; fix and resend.",
    "TWEAK": "Existing asset: change only what was asked. Use query_region for a bounded spatial working set. Apply the "
             "requested parameter edits with apply_scene_ir.",
    "VARIANTS": "Explore alternatives as variants (propose_variants), select each and run_visual_qa, promote the best.",
    "LOOKDEV": "The header's 'bound:' line lists the node ids by current material. Create materials with forge_pbr_material, "
               "whose prompt names only the material and its finish in a few words, never the object or its parts (the "
               "texture tiles over every face); tune and assign them with apply_shader_diff (assign_to takes those node ids), then "
               "render_lookdev with EEVEE/Cycles and run_visual_qa until Decisions approves (tier15 >= 3.4). With an image "
               "limit of 0, forge_pbr_material answers NO IMAGES: skip it; surfaces are made from the geometry at export. In an "
               "uncalibrated preview any lookdev review of the current scene lets it export; its findings ship with it.",
    "EXPORT": "Run generate_lods_and_colliders and run_usd_validator, then export_engine_package. Export is refused until QA "
              "approved the current scene; report what blocks it instead of retrying.",
}

_GEOMETRY_REVIEW = ("Then run_visual_qa; follow its action (auto_fix is applied for you; agent_repair names a target). "
                    "Stop once Tier-1 is OK and QA shows no geometry repair.")
_BLOCKOUT_REVIEW = ("Run run_visual_qa; fix primary forms, proportions and contact/clipping. Brief depiction scores finished "
                    "props: report secondary-detail deficiencies as deferred. Stop at Tier-1 OK with primary defects fixed. "
                    "Retain the QA verdict; no export approval.")

_SUBAGENT_TEXT = {
    "usd_verifier": ("Read-only check of the composed USD scene and engine compliance.",
                     "Inspect the manifest and run the USD validator; reply with the blocking issues in at most 3 lines."),
    "lookdev_critic": ("Read-only visual critique of the current asset via Decisions QA.",
                       "Run qa_probe and inspect the manifest; reply with the single most important fix in 2 lines."),
}


def _system(toolset: str, subagents: tuple[str, ...] | None = None, art: Any = None, *,
            authoring_intent: AuthoringIntent | str = AuthoringIntent.BLOCKOUT, repair: bool = False) -> str:
    """The whole system prompt of a stage agent (it replaces the SDK default; see STAGE_BEHAVIOR). ``subagents``
    defaults to the stage's own (``tools.STAGE_SUBAGENTS``). ``art``: the session's art direction; its author brief is
    added to the geometry stages and its texture brief to LOOKDEV (nothing is added without one)."""
    parts = [(f"You are the {toolset} stage agent of a game-asset pipeline. Nobody answers questions: work autonomously "
              "until the stage's stop condition holds, then reply with a short plain-text report of what changed and what "
              "remains. You edit the asset only through the tools; tool replies are one-line deltas; a refused or failed "
              "call says why in one line. Every prompt starts with a [STATE ...] header: trust it over older messages.")]
    if toolset in _IR_STAGES:
        parts.append(_IR_RULES)
    elif toolset in _MATERIAL_STAGES:
        parts.append(IR_SEMANTICS)
    if toolset == "BLOCKOUT":
        parts.append(stage_geometry_guidance(authoring_intent, repair=repair))
    parts.append(_STAGE_TEXT[toolset])
    if toolset == "BLOCKOUT":
        parts.append(_BLOCKOUT_REVIEW if AuthoringIntent(authoring_intent) is AuthoringIntent.BLOCKOUT else _GEOMETRY_REVIEW)
    if art is not None and toolset in _IR_STAGES and (line := author_text(art)):
        parts.append(line)
    elif art is not None and toolset in _MATERIAL_STAGES and (brief := texture_brief(art)):
        parts.append(f"Art direction {art.id} (forge_pbr_material adds it to every prompt): {brief}")
    if subs := (STAGE_SUBAGENTS.get(toolset, ()) if subagents is None else subagents):
        listed = "; ".join(f"{n}: {_SUBAGENT_TEXT[n][0].rstrip('.')}" for n in subs)
        parts.append(f"Subagents (invoke_subagent TypeName) run in their own context; a reply arrives as a message, do not "
                     f"poll. {listed}.")
    parts.append("Be terse.")
    return "\n".join(parts)


def _instructions(toolset: str, subagents: tuple[str, ...], art: Any = None, *,
                  authoring_intent: AuthoringIntent = AuthoringIntent.BLOCKOUT, repair: bool = False) -> types.CustomSystemInstructions:
    return types.CustomSystemInstructions(text=_system(toolset, subagents, art, authoring_intent=authoring_intent,
                                                       repair=repair), title="stage_agent")


def model_target(role: Role) -> ModelTarget:
    """``models.tier(role)`` -> ModelTarget with the tier's thinking level on the Gemini endpoint."""
    t = tier(role)
    opts = GeminiModelOptions(thinking_level=t.thinking) if t.thinking is not None else None
    return ModelTarget(name=t.model, types=[ModelType.TEXT], endpoint=GeminiAPIEndpoint(options=opts))


def _subagents(names: tuple[str, ...] | None = None) -> list[types.SubagentConfig]:
    """Read-only subagents (all of them, or ``names``), each told only its own job (same mechanisms as the stage)."""
    worker = tier("worker").model  # subagents take a model *name* and run on the agent-level endpoint options
    ro = types.SubagentCapabilities(agent_behavior=STAGE_BEHAVIOR, enabled_tools=[types.BuiltinTools.FINISH])
    return [types.SubagentConfig(
        name=name, description=desc, capabilities=ro, tools=list(SUBAGENT_TOOLS[name]), model=worker,
        system_instructions=types.CustomSystemInstructions(
            text=f"You are the {name} subagent of a game-asset pipeline; you never edit the asset. {job} Be terse.",
            title="subagent"))
        for name, (desc, job) in ((n, _SUBAGENT_TEXT[n]) for n in (names if names is not None else _SUBAGENT_TEXT))]


def build_stage_agent(
    stage: Toolset | str,
    session_id: str,
    *,
    registry: SceneSessionRegistry | None = None,
    role: Role | None = None,
    conversation_id: str | None = None,
    triggers: list[Any] | None = None,
    require_header: bool = True,
    subagents: bool = True,
    authoring_intent: AuthoringIntent | str = AuthoringIntent.BLOCKOUT,
    repair: bool = False,
) -> LocalAgentConfig:
    """LocalAgentConfig exposing only ``stage``'s tools (and its subagents, see ``tools.STAGE_SUBAGENTS``; none at all
    with ``subagents=False``), bound to ``session_id``.

    ``authoring_intent`` separates a completed prop or metric-led kit piece from an intermediate blockout; it
    affects BLOCKOUT instructions only. ``repair`` preserves existing detail and limits edits to the QA request.
    Neither changes the toolset, budget or QA gates.

    Side effects: sets the session's active stage/tool-set and binds the pinned conversation id
    (``cfg.conversation_id``) to the session so tools resolve it from ``ToolContext``.
    """
    registry = registry if registry is not None else DEFAULT_REGISTRY
    if stage not in STAGE_TOOLS:
        raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(STAGE_TOOLS)}")
    authoring_intent = AuthoringIntent(authoring_intent)
    s = session(registry, session_id)
    s.state.active_stage = TOOLSET_STAGE[stage]  # type: ignore[assignment]
    set_toolset(registry, session_id, stage)
    conv = conversation_id or str(uuid.uuid4())
    bind_conversation(conv, session_id, registry)
    subs = STAGE_SUBAGENTS[stage] if subagents else ()
    caps = types.CapabilitiesConfig(
        agent_behavior=STAGE_BEHAVIOR,
        enabled_tools=STAGE_BUILTINS + ([types.BuiltinTools.INVOKE_SUBAGENT] if subs else []),
        enable_subagents=bool(subs),
        tool_output_truncation_config=types.ToolOutputTruncationConfig(max_tokens=TOOL_OUTPUT_MAX_TOKENS),
        # allowed_subagents is advisory in the harness (not checked at call time); InvariantAndSafetyGate enforces it.
        **({"allowed_subagents": list(subs), "max_subagent_depth": 1} if subs else {}),
    )
    return LocalAgentConfig(
        system_instructions=_instructions(stage, subs, s.art, authoring_intent=authoring_intent, repair=repair),
        capabilities=caps,
        tools=list(STAGE_TOOLS[stage]),
        policies=policy.confirm_run_command(),  # backstop; the gate hook is the primary safety policy
        hooks=stage_hooks(registry, session_id, require_header=require_header),
        triggers=triggers or [],
        subagents=_subagents(subs) if subs else [],
        model=model_target(role or DEFAULT_ROLE[stage]),
        compaction_config=types.CompactionConfig(token_threshold=COMPACTION_TOKENS),
        workspaces=[str(s.workdir)],
        conversation_id=conv,  # pinned so tools can be bound before the first step
        session_continuation_mode=types.SessionContinuationMode.CREATE_OR_RESUME,  # default with an id is RESUME-only
    )


# --------------------------------------------------------------------------- director


class PartPlan(pydantic.BaseModel):
    name: str
    primitive: Literal["box", "cylinder", "cone", "sphere", "capsule", "lathe", "extrude", "sweep", "group", "mesh_ref"]
    approx_size_m: tuple[float, float, float]
    notes: str = ""


class MaterialPlan(pydantic.BaseModel):
    id: str
    description: str
    applies_to: list[str] = pydantic.Field(default_factory=list)


class AssetPlan(pydantic.BaseModel):
    """Turn-0 plan the director returns through the finish tool."""

    stages: list[Literal["BLOCKOUT", "VARIANTS", "LOOKDEV", "EXPORT"]]
    parts: list[PartPlan]
    materials: list[MaterialPlan]
    risks: list[str]


def build_director_agent(*, role: Role = "director", workdir: str | None = None) -> LocalAgentConfig:
    """Turn-0 planner: no custom tools, structured AssetPlan output."""
    return LocalAgentConfig(
        system_instructions=("You plan game-asset builds. Decompose the request into primitive parts (sizes in metres, Z-up), "
                             "materials and the stage list; list concrete risks (budget, floating parts, IP). Return the plan via finish."),
        capabilities=types.CapabilitiesConfig(enabled_tools=[types.BuiltinTools.FINISH], enable_subagents=False),
        response_schema=AssetPlan,
        model=model_target(role),
        **({"workspaces": [workdir]} if workdir else {}),
    )


# --------------------------------------------------------------------------- triggers


@dataclass
class CompletionEvent:
    kind: Literal["bake", "render", "export"]
    detail: str
    ok: bool = True


class BakeCompletionTrigger(_Shared):
    """Feeds external completion events (a long bake/render finishing) into a running agent.

    A Trigger is any async callable taking a ``TriggerContext``; the SDK's TriggerRunner starts it
    when the Agent session opens and cancels it on exit. Producers call :meth:`notify` (thread-safe
    via ``loop.call_soon_threadsafe`` when a loop is given); each event is delivered with
    ``ctx.send`` prefixed by the current state header so it passes ``StateHeaderHook``.
    """

    __name__ = "bake_completion"

    def __init__(self, registry: SceneSessionRegistry, session_id: str) -> None:
        self.registry, self.session_id = registry, session_id
        self.queue: asyncio.Queue[CompletionEvent] = asyncio.Queue()
        self.delivered: list[str] = []

    def notify(self, event: CompletionEvent, loop: asyncio.AbstractEventLoop | None = None) -> None:
        if loop is not None:
            loop.call_soon_threadsafe(self.queue.put_nowait, event)
        else:
            self.queue.put_nowait(event)

    async def __call__(self, ctx: TriggerContext) -> None:
        while True:
            ev = await self.queue.get()
            header = state_header(session(self.registry, self.session_id))
            msg = f"{header}\n\n[EVENT] {ev.kind} {'finished' if ev.ok else 'FAILED'}: {ev.detail[:200]}"
            await ctx.send(msg)
            self.delivered.append(msg)


# --------------------------------------------------------------------------- run_stage


@dataclass
class StageRun:
    text: str
    usage: types.UsageMetadata | None
    wall_s: float
    conversation_id: str
    telemetry: dict[str, Any] = field(default_factory=dict)
    stop_continuations: int = 0
    image_violations: list[str] = field(default_factory=list)


async def run_stage(
    stage: Toolset | str,
    session_id: str,
    prompt: types.Content,
    *,
    registry: SceneSessionRegistry | None = None,
    role: Role | None = None,
    timeout_s: float = 600.0,
    configure: Callable[[LocalAgentConfig], LocalAgentConfig] | None = None,
    **build_kwargs: Any,
) -> StageRun:
    """Opens the stage Agent, binds the conversation, sends one header-prefixed prompt, returns text+usage.

    ``configure`` may return a changed copy of the built config before the agent opens (the live benchmark points the
    model endpoint at its counting relay and switches retries off; tests point it at a local stub)."""
    registry = registry if registry is not None else DEFAULT_REGISTRY
    cfg = build_stage_agent(stage, session_id, registry=registry, role=role, **build_kwargs)
    if configure is not None:
        cfg = configure(cfg)
    conv = cfg.conversation_id or ""
    s = session(registry, session_id)
    hooks = {type(h): h for h in cfg.hooks}
    t0 = time.perf_counter()
    try:
        clean, _ = await sanitize_prompt(prompt, s)
        async with Agent(cfg) as agent:
            runner = getattr(agent, "_tool_runner", None)  # fallback binding if the harness renames the conversation
            ctx = getattr(runner, "_context", None)
            if ctx is not None:
                ctx.set_state(CTX_SESSION_KEY, session_id)
                ctx.set_state(CTX_REGISTRY_KEY, registry)
            resp = await asyncio.wait_for(agent.chat(with_state_header(clean, s)), timeout_s)
            text = await asyncio.wait_for(resp.text(), timeout_s)
            usage = resp.usage_metadata or agent.conversation.last_turn_usage
    finally:
        unbind_conversation(conv)
    tel = hooks.get(TelemetryHook)
    stop = hooks.get(QAStopHook)
    vis = hooks.get(VisionEvictionHook)
    return StageRun(text, usage, time.perf_counter() - t0, conv, tel.summary() if tel else {},
                    stop.continued if stop else 0, list(vis.violations) if vis else [])
