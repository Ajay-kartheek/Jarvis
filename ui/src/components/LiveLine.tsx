// The one line under the orb: what is happening right now. The user's words as they are recognised,
// the step Jarvis is on, or how to talk to it when nothing is going on. What Jarvis answers is not
// here: the answer has its own place below, in full (see Answer.tsx).

import { useLayoutEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { AppState } from "../api";
import { useSwap } from "../hooks";
import type { Model } from "../store";

export interface Live {
  kind: "hint" | "hearing" | "heard" | "step";
  text: string;
  /** Changes when the line should be replaced by a new one, rather than grow in place. */
  key: string;
  /** Still coming in: shown with a blinking caret. */
  typing: boolean;
}

const HINT_WAKE = 'Say "Hey Jarvis", or hold Control+Option+J';
const HINT_TALK = "Hold Control+Option+J and talk, or type";
const HINT_TYPE = "Type to talk to Jarvis";

/** How to start talking, given what this Mac can do right now. */
function hint({ ear, voice }: AppState): Live {
  const text = ear.on ? HINT_WAKE : voice.hear ? HINT_TALK : HINT_TYPE;
  return { kind: "hint", text, key: `hint:${text}`, typing: false };
}

/** What the live line says. */
export function useLive({ app, hearing, turn }: Model): Live {
  if (hearing.phase === "listening") return { kind: "hearing", text: hearing.text, key: "hearing", typing: true };
  if (hearing.phase === "heard") return { kind: "heard", text: hearing.text, key: "heard", typing: false };
  if (turn?.phase === "working") {
    const step = turn.steps[turn.steps.length - 1] ?? "Thinking";
    return { kind: "step", text: step, key: `step:${turn.steps.length}`, typing: false };
  }
  // Working on a message this window did not send, or one from before it was opened.
  if (app.busy && turn?.phase !== "streaming") return { kind: "step", text: "Working on it", key: "busy", typing: false };
  // Jarvis has answered and still listens for a moment, for what the user says next.
  if (app.ear.awake) return { kind: "hearing", text: "", key: "hearing", typing: true };
  return hint(app);
}

export function LiveLine({ live }: { live: Live }) {
  const empty = live.kind === "hearing" && !live.text;
  const box = useRef<HTMLDivElement>(null);
  const swap = useSwap(8);
  const [overflow, setOverflow] = useState(false);
  // Short text sits at the top of its box, under the orb. Words that outgrow the box are anchored to
  // the bottom, so that the newest ones stay in view while the user is still speaking.
  useLayoutEffect(() => {
    const text = box.current?.querySelector<HTMLElement>(".live-text");
    setOverflow(Boolean(text && box.current && text.offsetHeight > box.current.clientHeight + 1));
  });
  return (
    <div
      ref={box}
      className="live"
      data-kind={live.kind}
      data-overflow={overflow || undefined}
      aria-live={live.typing ? "off" : "polite"}
    >
      <AnimatePresence mode="wait" initial={false}>
        <motion.p
          key={live.key}
          className={live.kind === "step" ? "live-text thinking" : "live-text"}
          data-typing={live.typing || undefined}
          data-empty={empty || undefined}
          {...swap}
        >
          {empty ? "Listening" : live.text}
        </motion.p>
      </AnimatePresence>
    </div>
  );
}
