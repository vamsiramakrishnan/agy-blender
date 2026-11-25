import React, { useState } from 'react';
import {
  ANCHOR_RECEIPT,
  ANCHOR_SCENARIOS,
  DIGEST_MODES,
  FIELD_NEEDLE,
  FOUR_GATES,
  HARNESSES,
  MEASURED_RECEIPTS,
} from '../data/straitjacketData.js';

function CopyButton({ text, label = 'Copy' }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Fallback for non-secure local contexts
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        setCopied(true);
        setTimeout(() => setCopied(false), 1800);
      } catch {
        // ignore
      }
      document.body.removeChild(ta);
    }
  };

  return (
    <button
      type="button"
      className={`copy-btn ${copied ? 'is-copied' : ''}`}
      onClick={handleCopy}
      aria-label={`Copy ${label}`}
    >
      {copied ? 'Copied ✓' : label}
    </button>
  );
}

export function ActOverlays({
  digestMode,
  onDigestModeChange,
  anchorScenario,
  onAnchorScenarioChange,
  selectedHarnessIdx,
  onHarnessSelect,
  onJumpToAct,
}) {
  const activeDigest = DIGEST_MODES[digestMode] || DIGEST_MODES.digest;
  const activeAnchor = ANCHOR_SCENARIOS[anchorScenario] || ANCHOR_SCENARIOS.insert;
  const activeHarness = HARNESSES[selectedHarnessIdx] || HARNESSES[0];

  const quickstartCmd = [
    "python -m pip install 'ctx-harness==0.5.0'",
    'ctx setup && ctx doctor',
    'ctx run -- pytest -q',
  ].join('\n');

  return (
    <main className="scroll-runway" id="main-content">
      {/* =================================================================== */}
      {/* ACT 01 — HERO: RESTRAIN THE FLOOD, KEEP THE EVIDENCE                */}
      {/* =================================================================== */}
      <section className="act-section act-section--hero" id="act-hero" data-act="0">
        <div className="hero-layout">
          <div className="hero-top">
            <div className="eyebrow eyebrow--amber">
              <span className="eyebrow__tag">CTX-HARNESS v0.5.0</span>
              <span className="eyebrow__sep">·</span>
              <span>OPEN-SOURCE SIDECAR FOR CODING AGENTS</span>
            </div>
            <h1 className="hero-title">
              Restrain the flood.
              <br />
              <span className="hero-title__accent">Keep the evidence.</span>
            </h1>
            <p className="hero-subtitle">
              Straitjacket captures noisy tool output once in a local store and hands coding
              agents a deterministic digest plus a retrieval handle — never the raw transcript.
            </p>
          </div>

          <div className="hero-bottom">
            <div className="hero-actions">
              <a className="btn btn--primary" href="./docs/">
                <span>Read the Docs</span>
                <span aria-hidden="true">→</span>
              </a>
              <button
                type="button"
                className="btn btn--secondary"
                onClick={() => onJumpToAct(1)}
              >
                <span>See how it works</span>
                <span aria-hidden="true">↓</span>
              </button>
            </div>

            <div className="install-pill">
              <code className="mono">pip install &apos;ctx-harness==0.5.0&apos;</code>
              <CopyButton text="python -m pip install 'ctx-harness==0.5.0'" label="Copy" />
            </div>

            <div className="hero-formula mono" aria-label="Core invariant">
              <span>complete evidence ≠ active context</span>
              <span className="formula-arrow">→</span>
              <span className="text-amber">active context + addresses = reversible omission</span>
            </div>
          </div>
        </div>
      </section>

      {/* =================================================================== */}
      {/* ACT 02 — THE FLOOD: 302,628 TOKENS REPLAYED EVERY TURN              */}
      {/* =================================================================== */}
      <section className="act-section act-section--left" id="act-flood" data-act="1">
        <article className="evidence-panel evidence-panel--red">
          <div className="panel-kicker">
            <span className="mono text-red">02 · EVIDENCE FATES</span>
            <span className="panel-receipt mono">evals/field-needle-record.json</span>
          </div>
          <h2 className="panel-title">
            A 20,001-line log floods every turn — or truncation drops the needle.
          </h2>
          <p className="panel-copy">
            In the checked-in field-needle fixture ({FIELD_NEEDLE.bytes}), one quiet{' '}
            <code className="mono">INFO</code> anomaly sits at line{' '}
            <strong className="mono text-amber">{FIELD_NEEDLE.quietNeedleLine}</strong>. Passing
            the raw stream bills <strong>302,628 tokens</strong> on every subsequent turn.
            Head-and-tail truncation cuts it to 1,219 tokens and silently destroys the answer.
          </p>

          <div className="fate-list" role="list">
            {FIELD_NEEDLE.arms.map((arm) => {
              const barPct =
                arm.tokensNum > 100000 ? 100 : arm.tokensNum > 1000 ? 14 : 6;
              return (
                <div key={arm.name} className={`fate-row fate-row--${arm.tone}`} role="listitem">
                  <div className="fate-row__head">
                    <span className="fate-row__name">{arm.name}</span>
                    <span className={`fate-row__metric mono text-${arm.tone}`}>
                      {arm.tokens} <small>o200k tok</small>
                    </span>
                  </div>
                  <div className="fate-bar" aria-hidden="true">
                    <div
                      className={`fate-bar__fill fate-bar__fill--${arm.tone}`}
                      style={{ width: `${barPct}%` }}
                    />
                  </div>
                  <div className="fate-row__meta mono">
                    <span>Needle L14,238: <strong>{arm.needle}</strong></span>
                    <span>{arm.verdict}</span>
                  </div>
                </div>
              );
            })}
          </div>
        </article>
      </section>

      {/* =================================================================== */}
      {/* ACT 03 — FOUR GATES & ARTIFACT VAULT (INTERACTIVE)                  */}
      {/* =================================================================== */}
      <section className="act-section act-section--right" id="act-gates" data-act="2">
        <article className="evidence-panel evidence-panel--blue">
          <div className="panel-kicker">
            <span className="mono text-blue">03 · FOUR GATES &amp; LOCAL VAULT</span>
            <span className="panel-receipt mono">64 MiB/stream store</span>
          </div>
          <h2 className="panel-title">
            Capture once into the vault. Emit a typed digest with an address.
          </h2>
          <p className="panel-copy">
            Every byte passes four lifecycle gates — <strong>Birth</strong>, <strong>Entry</strong>,{' '}
            <strong>Residence</strong>, and <strong>Emission</strong> — backed by one local
            content-addressed store. Switch the delivery mode below to watch the 3D funnel and
            retrieval conduit respond live:
          </p>

          <div className="segmented-control" role="tablist" aria-label="Evidence delivery mode">
            {Object.values(DIGEST_MODES).map((mode) => (
              <button
                key={mode.id}
                type="button"
                role="tab"
                aria-selected={digestMode === mode.id}
                className={`seg-btn ${digestMode === mode.id ? `is-active seg-btn--${mode.tone}` : ''}`}
                onClick={() => onDigestModeChange(mode.id)}
              >
                <span>{mode.label}</span>
                <small className="mono">{mode.badge}</small>
              </button>
            ))}
          </div>

          <div className={`specimen-box specimen-box--${activeDigest.tone}`}>
            <div className="specimen-box__bar mono">
              <span>{activeDigest.label.toUpperCase()}</span>
              <span className={`text-${activeDigest.tone}`}>{activeDigest.badge}</span>
            </div>
            <pre className="specimen-pre mono">
              {activeDigest.snippet.map((line, i) => (
                <div
                  key={i}
                  className={
                    line.includes('L14238') || line.includes('ctx get')
                      ? 'specimen-line is-highlight'
                      : 'specimen-line'
                  }
                >
                  {line}
                </div>
              ))}
            </pre>
          </div>

          <div className="gates-mini-grid">
            {FOUR_GATES.map((g) => (
              <div key={g.num} className={`gate-chip gate-chip--${g.tone}`}>
                <span className={`mono text-${g.tone}`}>{g.num} {g.name}</span>
                <p>{g.detail}</p>
              </div>
            ))}
          </div>
        </article>
      </section>

      {/* =================================================================== */}
      {/* ACT 04 — CONTENT ANCHORS (INTERACTIVE LIVE EDIT SIMULATOR)          */}
      {/* =================================================================== */}
      <section className="act-section act-section--left" id="act-anchors" data-act="3">
        <article className="evidence-panel evidence-panel--green">
          <div className="panel-kicker">
            <span className="mono text-green">04 · CONTENT ANCHORS</span>
            <span className="panel-receipt mono">evals/anchor-drift-2026-08-20.json</span>
          </div>
          <h2 className="panel-title">
            Addresses that survive an edit — or refuse cleanly.
          </h2>
          <p className="panel-copy">
            A line number is a position, not an identity. In Straitjacket, live{' '}
            <code className="mono">repo:</code> ranges carry a short content digest (
            <code className="mono text-amber">@07407f1c</code>). Simulate an edit to watch the
            3D caliper track or refuse while the bare line pointer drifts:
          </p>

          <div className="segmented-control" role="tablist" aria-label="Anchor edit simulation">
            {Object.values(ANCHOR_SCENARIOS).map((scen) => (
              <button
                key={scen.id}
                type="button"
                role="tab"
                aria-selected={anchorScenario === scen.id}
                className={`seg-btn ${anchorScenario === scen.id ? `is-active seg-btn--${scen.statusTone}` : ''}`}
                onClick={() => onAnchorScenarioChange(scen.id)}
              >
                <span>{scen.shortLabel}</span>
              </button>
            ))}
          </div>

          <div className={`anchor-readout anchor-readout--${activeAnchor.statusTone}`}>
            <div className="anchor-readout__cmd mono">$ {activeAnchor.command}</div>
            <div className="anchor-readout__status">
              <span className={`badge badge--${activeAnchor.statusTone}`}>
                {activeAnchor.status}
              </span>
              <span className="anchor-readout__desc">{activeAnchor.description}</span>
            </div>
            <div className="anchor-compare mono">
              <div className="anchor-compare__row">
                <span className="text-red">Unanchored</span>
                <span>{activeAnchor.bareResult}</span>
              </div>
              <div className="anchor-compare__row">
                <span className={`text-${activeAnchor.statusTone}`}>Anchored</span>
                <span>{activeAnchor.anchoredResult}</span>
              </div>
            </div>
          </div>

          <div className="stat-strip">
            <div className="stat-cell">
              <strong className="mono text-green">{ANCHOR_RECEIPT.relocated}</strong>
              <span>Relocated</span>
            </div>
            <div className="stat-cell">
              <strong className="mono text-amber">{ANCHOR_RECEIPT.refused}</strong>
              <span>Cleanly refused</span>
            </div>
            <div className="stat-cell">
              <strong className="mono text-green">{ANCHOR_RECEIPT.anchoredWrong}</strong>
              <span>Wrong answers</span>
            </div>
            <div className="stat-cell">
              <strong className="mono text-red">{ANCHOR_RECEIPT.unanchoredWrong}</strong>
              <span>Bare-line errors</span>
            </div>
          </div>
        </article>
      </section>

      {/* =================================================================== */}
      {/* ACT 05 — STABLE PROMPT PREFIX & SYMBOL NAVIGATION                   */}
      {/* =================================================================== */}
      <section className="act-section act-section--right" id="act-prefix" data-act="4">
        <article className="evidence-panel evidence-panel--blue">
          <div className="panel-kicker">
            <span className="mono text-blue">05 · PROMPT PREFIX &amp; SYMBOLS</span>
            <span className="panel-receipt mono">deterministic local views</span>
          </div>
          <h2 className="panel-title">
            Freeze the prefix. Navigate by symbol, not whole files.
          </h2>
          <p className="panel-copy">
            Moving changing evidence to the front of a prompt invalidates provider caches.
            Straitjacket serializes fixed reference bytes ahead of changing queries and lets
            agents jump straight to definitions, call sites, and ranked starting packs.
          </p>

          <div className="prefix-diagram">
            <div className="prefix-block prefix-block--frozen">
              <span className="mono text-blue">CACHE_WRAP.PREFIX · IMMUTABLE</span>
              <p>Stable system instructions + reference material + shared tool surface. Byte-identical across questions.</p>
            </div>
            <div className="prefix-block prefix-block--dynamic">
              <span className="mono text-amber">CACHE_WRAP.SUFFIX · APPENDED</span>
              <p>Current question + bounded digest excerpts + targeted symbol spans only.</p>
            </div>
          </div>

          <div className="specimen-box specimen-box--blue">
            <div className="specimen-box__bar mono">
              <span>SYMBOL NAVIGATION &amp; PREFIX BUILDER</span>
              <span className="text-blue">CLI SPECIMEN</span>
            </div>
            <pre className="specimen-pre mono">
              <div className="specimen-line">$ ctx search repo: sym:parse_ref</div>
              <div className="specimen-line">$ ctx def repo:src/ctx/refs.py:parse_ref</div>
              <div className="specimen-line">$ ctx refs parse_ref --path src/ctx</div>
              <div className="specimen-line is-highlight">
                $ ctx compress build.log --query &apos;Which target failed?&apos; --cache-prefix ref.txt
              </div>
            </pre>
          </div>
        </article>
      </section>

      {/* =================================================================== */}
      {/* ACT 06 — CROSS-HARNESS ORCHESTRATION & ACP RELAY                    */}
      {/* =================================================================== */}
      <section className="act-section act-section--left" id="act-harnesses" data-act="5">
        <article className="evidence-panel evidence-panel--amber">
          <div className="panel-kicker">
            <span className="mono text-amber">06 · ACP ORCHESTRATION &amp; RELAY</span>
            <span className="panel-receipt mono">7 supported harnesses</span>
          </div>
          <h2 className="panel-title">
            Delegate across seven harnesses. Pass handles, never transcripts.
          </h2>
          <p className="panel-copy">
            <code className="mono">ctx orchestrate</code> routes exploration, implementation, and
            verification to eligible harnesses over Agent Client Protocol v1. Workers coordinate
            through an append-only <strong>Task Ledger</strong> and deterministic{' '}
            <strong>Steward</strong>; <code className="mono">ctx relay</code> signals peer
            addresses — and refuses raw prose. Click a harness below or tap any 3D pod:
          </p>

          <div className="harness-grid" role="tablist" aria-label="Supported coding harnesses">
            {HARNESSES.map((h, idx) => (
              <button
                key={h.id}
                type="button"
                role="tab"
                aria-selected={selectedHarnessIdx === idx}
                className={`harness-pill ${selectedHarnessIdx === idx ? 'is-active' : ''}`}
                style={{ '--harness-accent': h.color }}
                onClick={() => onHarnessSelect(idx)}
              >
                <img src={h.icon} alt="" width="20" height="20" className="harness-pill__icon" />
                <span>{h.name}</span>
              </button>
            ))}
          </div>

          <div
            className="harness-detail"
            style={{ '--harness-accent': activeHarness.color }}
          >
            <div className="harness-detail__head">
              <img
                src={activeHarness.icon}
                alt={activeHarness.name}
                width="28"
                height="28"
                className="harness-detail__logo"
              />
              <div>
                <h3>{activeHarness.name}</h3>
                <span className="harness-detail__role">{activeHarness.role}</span>
              </div>
            </div>
            <div className="harness-detail__rows mono">
              <div>
                <span className="label">ACP endpoint</span>
                <code>{activeHarness.acp}</code>
              </div>
              <div>
                <span className="label">Host hooks</span>
                <span>{activeHarness.nativeHook}</span>
              </div>
            </div>
          </div>

          <div className="specimen-box specimen-box--amber">
            <div className="specimen-box__bar mono">
              <span>RELAY SIGNAL · ADDRESSES ONLY</span>
              <span className="text-amber">ACP v1 + MCP</span>
            </div>
            <pre className="specimen-pre mono">
              <div className="specimen-line">
                $ ctx orchestrate &apos;Review the current diff&apos; --dry-run
              </div>
              <div className="specimen-line is-highlight">
                $ ctx relay signal codex repo:README.md --origin opencode --note &apos;Check setup&apos;
              </div>
            </pre>
          </div>
        </article>
      </section>

      {/* =================================================================== */}
      {/* ACT 07 — RECEIPTS, QUICKSTART & DOCUMENTATION GATEWAY               */}
      {/* =================================================================== */}
      <section className="act-section act-section--finale" id="act-receipts" data-act="6">
        <div className="finale-grid">
          <div className="finale-header">
            <div className="eyebrow eyebrow--amber">
              <span className="eyebrow__tag">07 · RECEIPTS BEFORE SPECTACLE</span>
              <span className="eyebrow__sep">·</span>
              <span>MEASURED IN REPOSITORY EVALS</span>
            </div>
            <h2 className="finale-title">
              Honest measurements. Deterministic tools. Ready in one command.
            </h2>
            <p className="finale-subtitle">
              Straitjacket is not a process sandbox, not agent memory, and does not claim every
              small task gets cheaper. Every number below comes from a checked-in repository
              receipt — including where overhead raised cost.
            </p>
          </div>

          <div className="receipts-table" role="region" aria-label="Measured evaluation receipts">
            {MEASURED_RECEIPTS.map((row) => (
              <div key={row.workload} className="receipt-row">
                <div className="receipt-row__main">
                  <div className="receipt-row__workload">{row.workload}</div>
                  <div className="receipt-row__detail">{row.detail}</div>
                  <div className="receipt-row__source mono">{row.source}</div>
                </div>
                <div className={`receipt-row__metric mono text-${row.tone}`}>
                  {row.metric}
                </div>
              </div>
            ))}
          </div>

          <div className="quickstart-card">
            <div className="quickstart-card__head">
              <div>
                <span className="mono text-amber">QUICKSTART · PYTHON 3.11+</span>
                <h3>Try it on your next failing test</h3>
              </div>
              <CopyButton text={quickstartCmd} label="Copy snippet" />
            </div>
            <pre className="quickstart-pre mono">{quickstartCmd}</pre>

            <div className="docs-gateway">
              <a className="btn btn--primary btn--lg" href="./docs/">
                <span>Open Full Documentation</span>
                <span aria-hidden="true">→</span>
              </a>
              <div className="docs-sublinks">
                <a href="./docs/start/getting-started/">Getting Started</a>
                <a href="./docs/start/how-it-works/">How It Works</a>
                <a href="./docs/guides/acp/">ACP Orchestration</a>
                <a href="./docs/guides/anchors/">Content Anchors</a>
              </div>
            </div>
          </div>

          <footer className="site-footer mono">
            <div className="site-footer__brand">
              <img src="./icon.svg" alt="" width="18" height="18" />
              <span>straitjacket · ctx-harness v0.5.0 · Apache-2.0</span>
            </div>
            <div className="site-footer__links">
              <a href="./docs/">Documentation</a>
              <span>·</span>
              <span>Your harness keeps model auth &amp; permissions</span>
            </div>
          </footer>
        </div>
      </section>
    </main>
  );
}
