import React, { useCallback, useEffect, useRef, useState } from 'react';
import Lenis from 'lenis';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { EvidenceScene } from './scene/EvidenceScene.js';
import { ACTS } from './data/straitjacketData.js';
import { TopNav } from './components/TopNav.jsx';
import { ChapterRail } from './components/ChapterRail.jsx';
import { LoadingCurtain } from './components/LoadingCurtain.jsx';
import { ActOverlays } from './components/ActOverlays.jsx';

gsap.registerPlugin(ScrollTrigger);

/**
 * Computes exact DOM-anchored continuous act progress in [0, 1] by measuring the
 * actual centers of the 7 [data-act] sections. Guarantees that on BOTH desktop and
 * phone viewports, whenever Act k is centered in view, 3D camera progress equals k/6.
 */
function computeDomActProgress() {
  const sections = Array.from(document.querySelectorAll('[data-act]'));
  if (sections.length < 2) return { normalized: 0, activeIdx: 0 };

  const vh = window.innerHeight || 1;
  const scrollY = window.scrollY;
  const maxScroll = Math.max(1, document.documentElement.scrollHeight - vh);

  if (scrollY <= 2) return { normalized: 0, activeIdx: 0 };
  if (scrollY >= maxScroll - 2) {
    return { normalized: 1, activeIdx: sections.length - 1 };
  }

  // Anchor points: scrollY at which each section is ideally framed
  const anchors = sections.map((el, idx) => {
    const top = el.offsetTop;
    if (idx === 0) return 0;
    if (idx === sections.length - 1) return Math.min(maxScroll, top);
    // Align section center to viewport center (or section top near nav on mobile)
    const ideal = top + el.offsetHeight * 0.5 - vh * 0.5;
    return Math.max(0, Math.min(maxScroll, ideal));
  });

  // Ensure strictly non-decreasing anchors
  for (let i = 1; i < anchors.length; i++) {
    if (anchors[i] <= anchors[i - 1]) anchors[i] = anchors[i - 1] + 1;
  }

  let actFloat = 0;
  for (let i = 0; i < anchors.length - 1; i++) {
    if (scrollY >= anchors[i] && scrollY <= anchors[i + 1]) {
      const local = (scrollY - anchors[i]) / Math.max(1, anchors[i + 1] - anchors[i]);
      actFloat = i + local;
      break;
    } else if (scrollY > anchors[anchors.length - 1]) {
      actFloat = anchors.length - 1;
    }
  }

  const normalized = Math.max(0, Math.min(1, actFloat / (sections.length - 1)));
  const activeIdx = Math.max(0, Math.min(sections.length - 1, Math.round(actFloat)));
  return { normalized, activeIdx };
}

export function App() {
  const canvasRef = useRef(null);
  const sceneRef = useRef(null);
  const lenisRef = useRef(null);

  const [loadProgress, setLoadProgress] = useState(0);
  const [sceneReady, setSceneReady] = useState(false);
  const [scrollProgress, setScrollProgress] = useState(0);
  const [activeActIdx, setActiveActIdx] = useState(0);

  const [digestMode, setDigestMode] = useState('digest');
  const [anchorScenario, setAnchorScenario] = useState('insert');
  const [selectedHarnessIdx, setSelectedHarnessIdx] = useState(0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;

    const engine = new EvidenceScene(canvas, {
      onProgress: (p) => setLoadProgress(p),
      onReady: () => {
        setLoadProgress(1);
        setSceneReady(true);
      },
      onAutoAnchorScenario: (scen) => setAnchorScenario(scen),
      onHarnessSelect: (idx) => setSelectedHarnessIdx(idx),
    });
    sceneRef.current = engine;

    return () => {
      engine.dispose();
      sceneRef.current = null;
    };
  }, []);

  useEffect(() => {
    const lenis = new Lenis({
      duration: 1.15,
      easing: (t) => Math.min(1, 1.001 - Math.pow(2, -10 * t)),
      orientation: 'vertical',
      gestureOrientation: 'vertical',
      smoothWheel: true,
      wheelMultiplier: 0.95,
      touchMultiplier: 1.35,
    });
    lenisRef.current = lenis;

    const syncScroll = () => {
      const { normalized, activeIdx } = computeDomActProgress();
      setScrollProgress(normalized);
      setActiveActIdx(activeIdx);
      if (sceneRef.current) {
        sceneRef.current.setScrollProgress(normalized);
      }
      ScrollTrigger.update();
    };

    lenis.on('scroll', syncScroll);
    window.addEventListener('scroll', syncScroll, { passive: true });
    window.addEventListener('resize', syncScroll);
    syncScroll();

    let rafId = 0;
    const raf = (time) => {
      lenis.raf(time);
      rafId = requestAnimationFrame(raf);
    };
    rafId = requestAnimationFrame(raf);

    const ctx = gsap.context(() => {
      gsap.utils.toArray('.evidence-panel').forEach((panel) => {
        gsap.fromTo(
          panel,
          { opacity: 0.35, y: 22 },
          {
            opacity: 1,
            y: 0,
            duration: 0.65,
            ease: 'power3.out',
            scrollTrigger: {
              trigger: panel,
              start: 'top 88%',
              end: 'top 52%',
              scrub: 0.35,
            },
          }
        );
      });
    });

    return () => {
      ctx.revert();
      cancelAnimationFrame(rafId);
      window.removeEventListener('scroll', syncScroll);
      window.removeEventListener('resize', syncScroll);
      lenis.destroy();
      lenisRef.current = null;
    };
  }, []);

  const handleDigestModeChange = useCallback((mode) => {
    setDigestMode(mode);
    if (sceneRef.current) sceneRef.current.setDigestMode(mode);
  }, []);

  const handleAnchorScenarioChange = useCallback((scen) => {
    setAnchorScenario(scen);
    if (sceneRef.current) sceneRef.current.setAnchorScenario(scen, true);
  }, []);

  const handleHarnessSelect = useCallback((idx) => {
    setSelectedHarnessIdx(idx);
    if (sceneRef.current) sceneRef.current.setSelectedHarness(idx);
  }, []);

  const handleJumpToAct = useCallback((idx) => {
    const act = ACTS[idx];
    if (!act) return;
    const targetEl = document.getElementById(`act-${act.id}`);
    if (targetEl && lenisRef.current) {
      lenisRef.current.scrollTo(targetEl, { offset: 0, duration: 1.25 });
    } else if (targetEl) {
      targetEl.scrollIntoView({ behavior: 'smooth' });
    }
  }, []);

  const activeAct = ACTS[activeActIdx] || ACTS[0];

  return (
    <div className="app-shell">
      <canvas
        ref={canvasRef}
        className="webgl-canvas"
        aria-label="Interactive 3D visualization of Straitjacket's evidence containment, four gates, content anchors, prompt prefix stability, and cross-harness ACP orchestration"
      />

      <LoadingCurtain progress={loadProgress} ready={sceneReady} />

      <TopNav
        activeAct={activeAct}
        scrollProgress={scrollProgress}
        onJumpToAct={handleJumpToAct}
      />

      <ChapterRail activeIdx={activeActIdx} onJumpToAct={handleJumpToAct} />

      <ActOverlays
        digestMode={digestMode}
        onDigestModeChange={handleDigestModeChange}
        anchorScenario={anchorScenario}
        onAnchorScenarioChange={handleAnchorScenarioChange}
        selectedHarnessIdx={selectedHarnessIdx}
        onHarnessSelect={handleHarnessSelect}
        onJumpToAct={handleJumpToAct}
      />
    </div>
  );
}
