import React from 'react';

export function LoadingCurtain({ progress, ready }) {
  const pct = Math.round(progress * 100);
  return (
    <div
      className={`loading-curtain ${ready ? 'is-hidden' : ''}`}
      aria-hidden={ready}
    >
      <div className="loading-curtain__panel">
        <div className="loading-curtain__brand">
          <img src="./icon.svg" alt="" width="34" height="34" />
          <span>STRAITJACKET</span>
        </div>
        <div className="loading-curtain__bar">
          <div className="loading-curtain__fill" style={{ width: `${Math.max(8, pct)}%` }} />
        </div>
        <div className="loading-curtain__meta">
          <span>MOUNTING EVIDENCE MACHINE</span>
          <span className="mono">{pct}%</span>
        </div>
      </div>
    </div>
  );
}
