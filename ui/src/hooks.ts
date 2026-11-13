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

/**
 * True for `ms` after the local clock time `since`, then false. `null` is false. It re-renders once,
 * when the time runs out, so a line that lingers for a few seconds does not need a ticking clock.
 */
export function useWhile(since: number | null, ms: number): boolean {
  const [, expire] = useState(0);
  useEffect(() => {
    if (since === null) return;
    const left = since + ms - Date.now();
    if (left <= 0) return;
    const timer = setTimeout(() => expire((n) => n + 1), left + 20);
    return () => clearTimeout(timer);
  }, [since, ms]);
  return since !== null && Date.now() - since < ms;
}

/**
 * Motion props for something that is swapped for another in place: the new one fades and slides in, the
 * old one fades out first. With reduced motion it is only the swap, and nothing waits for an animation
 * to finish before the new one shows.
 */
export function useSwap(shift = 8) {
  const reduced = useReducedMotion();
  if (reduced) return { initial: false as const, animate: { opacity: 1, y: 0 } };
  return {
    initial: { opacity: 0, y: shift },
    animate: { opacity: 1, y: 0 },
    exit: { opacity: 0, y: -shift * 0.75, transition: { duration: 0.12 } },
  };
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

/**
 * Whether the content of `ref` is taller than `limit` px. It follows the element as it is resized, so a
 * window made narrower, which wraps the text onto more lines, turns it on.
 */
export function useTallerThan(limit: number): [ref: (element: HTMLElement | null) => void, taller: boolean] {
  const [element, setElement] = useState<HTMLElement | null>(null);
  const [taller, setTaller] = useState(false);
  useEffect(() => {
    if (!element) return;
    const check = () => setTaller(element.scrollHeight > limit + 4);
    check();
    const watch = new ResizeObserver(check);
    watch.observe(element);
    return () => watch.disconnect();
  }, [element, limit]);
  return [setElement, taller];
}
