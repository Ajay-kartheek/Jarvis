import { useEffect, useRef } from "react";
import { useReducedMotion } from "motion/react";
import { levels } from "../store";

const BARS = 64;
const FRAME_MS = 1000 / 30;
const STALE_MS = 500; // no level for this long means silence
const SOURCES = ["system", "mic"] as const;
const SPLIT = 0.62; // share of the height given to their audio; keep in step with .wave in styles.css

/**
 * Scrolling level meter drawn from the `level` messages: their audio rises above the centre line,
 * the microphone hangs below it. Under reduced motion it becomes a still meter refreshed a few times a second.
 */
export function Waveform() {
  const ref = useRef<HTMLCanvasElement>(null);
  const reduced = useReducedMotion();

  useEffect(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const color = getComputedStyle(canvas).color;
    const bars = { system: new Float32Array(BARS), mic: new Float32Array(BARS) };
    const eased = { system: 0, mic: 0 };
    let quietFrames = 0;

    const draw = () => {
      const now = performance.now();
      for (const source of SOURCES) {
        const { level, at } = levels[source];
        // Square root lifts quiet speech into view; raw levels sit low in the 0 to 1 range.
        const target = now - at < STALE_MS ? Math.sqrt(Math.min(1, Math.max(0, level))) : 0;
        eased[source] += (target - eased[source]) * 0.4;
        if (reduced) {
          bars[source].fill(target);
        } else {
          bars[source].copyWithin(0, 1);
          bars[source][BARS - 1] = eased[source] * (0.55 + 0.45 * Math.random());
        }
      }
      quietFrames = eased.system + eased.mic < 0.005 ? quietFrames + 1 : 0;
      if (quietFrames > BARS + 1) return; // everything has scrolled away; the canvas is already blank

      const scale = window.devicePixelRatio || 1;
      const width = Math.round(canvas.clientWidth * scale);
      const height = Math.round(canvas.clientHeight * scale);
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = color;
      const pitch = width / BARS;
      const mid = Math.round(height * SPLIT); // matches the centre line drawn in CSS
      for (let i = 0; i < BARS; i++) {
        const fade = 0.25 + (0.75 * i) / BARS; // older bars dim towards the left
        const up = bars.system[i] * (mid - scale);
        const down = bars.mic[i] * (height - mid - scale);
        ctx.globalAlpha = fade;
        ctx.fillRect(i * pitch, mid - up, pitch * 0.55, up);
        ctx.globalAlpha = fade * 0.5;
        ctx.fillRect(i * pitch, mid + scale, pitch * 0.55, down);
      }
    };

    if (reduced) {
      const timer = setInterval(draw, 250);
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

  return <canvas ref={ref} className="wave" aria-hidden="true" />;
}
