import { useEffect, useRef, useState } from "react";
import { useReducedMotion } from "motion/react";

/** The current time in ms, rounded down to `step`; re-renders once per step. */
export function useNow(step = 1000): number {
  const [tick, setTick] = useState(() => Math.floor(Date.now() / step));
  useEffect(() => {
    const timer = setInterval(() => setTick(Math.floor(Date.now() / step)), Math.max(50, step / 4));
    return () => clearInterval(timer);
  }, [step]);
  return tick * step;
}

interface RevealOptions {
  /** Slowest typing speed, in characters a second. */
  cps?: number;
  /** How hard to chase a backlog: each second the gap shrinks by about this factor. */
  catchUp?: number;
  /** Show everything at once if the text is already complete when the component mounts. */
  instant?: boolean;
}

/**
 * Typewriter position: how many of `total` characters to show. It chases `total` as streamed text
 * grows, starts again from zero whenever `key` changes, and is always `total` under reduced motion.
 */
export function useReveal(
  total: number,
  key: unknown,
  { cps = 90, catchUp = 8, instant = false }: RevealOptions = {},
): number {
  const reduced = useReducedMotion();
  const [shown, setShown] = useState({ key, n: instant ? total : 0 });
  const pos = useRef({ key, n: instant ? total : 0 });

  useEffect(() => {
    if (pos.current.key !== key) pos.current = { key, n: 0 };
    const p = pos.current;
    if (reduced || p.n >= total) return;
    let frame = 0;
    let last = performance.now();
    const tick = (time: number) => {
      const dt = Math.min(0.1, (time - last) / 1000);
      last = time;
      p.n = Math.min(total, p.n + Math.max(cps, (total - p.n) * catchUp) * dt);
      setShown({ key, n: Math.floor(p.n) });
      if (p.n < total) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [total, key, reduced, cps, catchUp]);

  if (reduced) return total;
  return shown.key === key ? Math.min(shown.n, total) : 0;
}
