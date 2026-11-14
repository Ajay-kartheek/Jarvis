// What the orb is doing, and how hard a real sound is driving it. Kept apart from the 3D scene so the
// rest of the page can use both without loading the 3D library.

import type { Voice } from "../audio";
import { levels, voiceOut } from "../levels";
import type { Model } from "../store";

/**
 * `ready`: nothing is going on, and the microphone is open for "Hey Jarvis": calm, but visibly there.
 * `call`: following a call quietly, small and calm, while the SAY card has the stage.
 */
export type OrbState = "idle" | "ready" | "listening" | "thinking" | "speaking" | "call";

/** How fast the instrument ring turns in each state, as a multiple of its idle speed. */
export const RING_RATE: Record<OrbState, number> = { idle: 1, ready: 1.3, listening: 1.8, thinking: 6, speaking: 2.4, call: 0.6 };

const STALE_MS = 500; // no level for this long means silence

/**
 * Loudness to follow right now, 0 to 1: the user's voice while Jarvis listens, Jarvis's own voice
 * while it speaks. Zero when there is no sound to follow.
 */
export function drive(state: OrbState): number {
  if (state === "listening") {
    const { level, at } = levels.voice;
    // Square root lifts quiet speech into view; raw levels sit low in the 0 to 1 range.
    return performance.now() - at < STALE_MS ? Math.sqrt(Math.min(1, Math.max(0, level))) : 0;
  }
  return state === "speaking" ? voiceOut.level : 0;
}

/**
 * What the orb is doing. A call outranks everything, then the user's voice, then Jarvis speaking
 * (a reply that is only shown counts too), then working. With none of those, an ear that is still
 * awake is listening for a follow-up, and one that is open but asleep is waiting for its name.
 */
export function orbStateOf({ app, hearing, turn }: Model, voice: Voice): OrbState {
  if (app.status !== "idle" || app.meeting) return "call";
  if (hearing.phase === "listening") return "listening";
  if (voice.speaking || turn?.phase === "streaming") return "speaking";
  if (app.busy || hearing.phase === "heard" || turn?.phase === "working") return "thinking";
  if (app.ear.awake) return "listening";
  return app.ear.on ? "ready" : "idle";
}
