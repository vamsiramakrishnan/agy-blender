import React from 'react';

export function TopNav({ activeAct, scrollProgress, onJumpToAct }) {
  return (
    <header className="top-nav" role="banner">
      <div className="top-nav__inner">
        <a
          href="#act-hero"
          className="brand-lockup"
          onClick={(e) => {
            e.preventDefault();
            onJumpToAct(0);
          }}
          aria-label="Straitjacket home"
        >
          <img src="./icon.svg" alt="" width="26" height="26" className="brand-lockup__icon" />
          <span className="brand-lockup__wordmark">straitjacket</span>
          <span className="brand-lockup__ver">v0.5.0</span>
        </a>

        <div className="top-nav__telemetry" aria-live="polite">
          <span className={`status-dot status-dot--${activeAct.semantic}`} />
          <span className="top-nav__index">{activeAct.index}</span>
          <span className="top-nav__sep">/</span>
          <span className="top-nav__label">{activeAct.telemetry}</span>
        </div>

        <nav className="top-nav__actions" aria-label="Primary navigation">
          <button
            type="button"
            className="nav-link nav-link--ghost"
            onClick={() => onJumpToAct(6)}
          >
            Receipts
          </button>
          <a className="nav-cta" href="./docs/">
            <span>Docs</span>
            <span aria-hidden="true">→</span>
          </a>
        </nav>
      </div>
      <div className="top-nav__progress" aria-hidden="true">
        <div
          className={`top-nav__progress-bar top-nav__progress-bar--${activeAct.semantic}`}
          style={{ transform: `scaleX(${Math.max(0.015, scrollProgress)})` }}
        />
      </div>
    </header>
  );
}
