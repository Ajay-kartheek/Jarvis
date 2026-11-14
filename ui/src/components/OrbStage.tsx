// The orb as the page shows it: the 3D scene, a ring of instruments turning around it, and two
// wings of bars that follow the sound. The ring, the wings and the glow need no 3D, so the orb
// still reads when the 3D scene cannot start.

import { Suspense, lazy, memo, useEffect, useRef } from "react";
import { motion, useReducedMotion } from "motion/react";
import { Guard } from "./Guard";
import { HudRing, useRingRate } from "./HudRing";
import { type OrbState, RING_RATE, drive } from "./orbState";

const Orb = lazy(() => import("./Orb"));

const LABEL: Record<OrbState, string> = {
  idle: "Standing by",
  ready: "Listening for you",
  listening: "Listening",
  thinking: "Working",
  speaking: "Speaking",
  call: "In a call",
};
const FRAME_MS = 1000 / 30;
const BARS = 22; // per wing
const PITCH = 6; // px from one bar to the next

interface Props {
  state: OrbState;
  /** Small and quiet, beside the call's title: no wings, no label. */
  compact?: boolean;
  /** Said under the orb in place of the state's own words. */
  label?: string;
}

export const OrbStage = memo(function OrbStage({ state, compact = false, label }: Props) {
  const stage = useRef<HTMLDivElement>(null);
  const core = useRef<HTMLDivElement>(null);
  const wings = useRef<HTMLCanvasElement>(null);
  const reduced = Boolean(useReducedMotion());
  const live = useRef({ state, compact });
  live.current = { state, compact };
  useRingRate(stage, RING_RATE[state]);

  // One loop feeds both the glow (through --level) and the wings.
  useEffect(() => {
    const canvas = wings.current;
    const box = stage.current;
    const orb = core.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !box || !orb || !ctx) return;
    const color = getComputedStyle(canvas).color;
    const trail = new Float32Array(BARS); // loudness, newest nearest the orb
    let eased = 0;
    let phase = 0;
    let stillFrames = 0;
    let shown = "";

    const draw = () => {
      const now = live.current.state;
      let target = drive(now);
      // A reply that is shown but not spoken, and thinking, still move a little, by themselves.
      if (target < 0.02 && !reduced) {
        phase += 0.11;
        if (now === "speaking") target = 0.3 + 0.26 * Math.sin(phase * 3.1) * Math.sin(phase * 1.3);
        else if (now === "thinking") target = 0.1 + 0.07 * Math.sin(phase * 5);
      }
      eased += (Math.max(0, target) - eased) * 0.45;
      const level = eased.toFixed(3);
      if (level !== shown) box.style.setProperty("--level", (shown = level)); // only when it moved: styles are not free
      trail.copyWithin(1, 0);
      trail[0] = reduced ? eased : eased * (0.55 + 0.45 * Math.random());
      stillFrames = eased < 0.004 ? stillFrames + 1 : 0;
      if (stillFrames > BARS + 2) return; // the last bar has travelled out; the picture cannot change

      const scale = window.devicePixelRatio || 1;
      const width = Math.round(canvas.clientWidth * scale);
      const height = Math.round(canvas.clientHeight * scale);
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      ctx.clearRect(0, 0, width, height);
      if (live.current.compact) return;
      const diameter = orb.offsetHeight * scale;
      const mid = (orb.offsetTop + orb.offsetHeight / 2) * scale;
      const inner = diameter / 2 + 5 * scale; // the wings start just outside the instrument ring
      const pitch = PITCH * scale;
      const count = Math.min(BARS, Math.floor((width / 2 - inner - 3 * scale) / pitch));
      ctx.fillStyle = color;
      for (let i = 0; i < count; i++) {
        const fade = 1 - i / count;
        const half = Math.max(scale * 0.75, trail[i] * diameter * 0.3 * (0.35 + 0.65 * fade));
        const offset = inner + i * pitch;
        ctx.globalAlpha = 0.18 + 0.72 * fade * Math.min(1, trail[i] * 3 + 0.12);
        ctx.fillRect(width / 2 + offset, mid - half, pitch * 0.42, half * 2);
        ctx.fillRect(width / 2 - offset - pitch * 0.42, mid - half, pitch * 0.42, half * 2);
      }
    };

    if (reduced) {
      const timer = setInterval(draw, 250);
      draw();
      return () => clearInterval(timer);
    }
    let frame = 0;
    let last = 0;
    const loop = (time: number) => {
      frame = requestAnimationFrame(loop);
      if (document.hidden || time - last < FRAME_MS - 2) return;
      last = time;
      draw();
    };
    frame = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frame);
  }, [reduced]);

  return (
    <div ref={stage} className="orb" data-state={state} data-compact={compact || undefined}>
      <canvas ref={wings} className="orb-wings" aria-hidden="true" />
      <div ref={core} className="orb-core">
        <i className="orb-halo" />
        <HudRing />
        {/* Each change of state sends one ring outwards; with reduced motion the label and colours say it. */}
        {!reduced && (
          <motion.i
            key={state}
            className="orb-pulse"
            initial={{ scale: 0.6, opacity: 0.7 }}
            animate={{ scale: 1.1, opacity: 0 }}
            transition={{ duration: 0.9, ease: "easeOut" }}
          />
        )}
        {/* If the 3D canvas cannot start (no WebGL), everything around it still shows the state. */}
        <Guard>
          <Suspense fallback={null}>
            <Orb state={state} />
          </Suspense>
        </Guard>
      </div>
      <motion.span
        key={label ?? state}
        className="orb-state"
        data-note={label ? "" : undefined}
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
      >
        {label ?? LABEL[state]}
      </motion.span>
    </div>
  );
});
