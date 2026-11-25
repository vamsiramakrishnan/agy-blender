import React from 'react';
import { ACTS } from '../data/straitjacketData.js';

export function ChapterRail({ activeIdx, onJumpToAct }) {
  return (
    <nav className="chapter-rail" aria-label="Story chapters">
      {ACTS.map((act, idx) => {
        const isActive = idx === activeIdx;
        return (
          <button
            key={act.id}
            type="button"
            className={`chapter-rail__item ${isActive ? 'is-active' : ''} chapter-rail__item--${act.semantic}`}
            onClick={() => onJumpToAct(idx)}
            aria-current={isActive ? 'step' : undefined}
            title={`${act.index} · ${act.label}`}
          >
            <span className="chapter-rail__label">{act.shortLabel}</span>
            <span className="chapter-rail__tick" />
          </button>
        );
      })}
    </nav>
  );
}
