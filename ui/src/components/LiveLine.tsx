// The one line under the orb: what is happening right now. The user's words as they are recognised,
// the step Jarvis is on, the sentence it is saying, or the hint when nothing is going on.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { Voice } from "../audio";
import { useSwap, useWhile } from "../hooks";
import type { Model } from "../store";

export interface Live {
  kind: "hint" | "hearing" | "heard" | "step" | "reply" | "error";
  text: string;
  /** Changes when the line should be replaced by a new one, rather than grow in place. */
  key: string;
  /** Still coming in: shown with a blinking caret. */
  typing: boolean;
}

const HINT_TALK = "Hold Control+Option+J and talk, or type";
const HINT_TYPE = "Type to talk to Jarvis";
const ERROR_MS = 8000;
const VOICE_WAIT_MS = 2500; // a reply that is to be spoken waits this long for its first sentence of sound

/** How long a finished reply stays up: long enough to read, longer for more words. */
const stays = (text: string) => Math.min(12_000, 3500 + text.length * 40);

/**
 * What the live line says. A spoken reply is shown a sentence at a time, as it is said, and the line is
 * the hint again once the voice is done. A typed one streams in as text and stays up for a few
 * seconds. Either way the reply itself stays in the conversation below.
 */
export function useLive({ app, hearing, turn }: Model, voice: Voice): Live {
  const failed = turn?.phase === "error";
  const recent = useWhile(turn?.doneAt ?? null, failed ? ERROR_MS : stays(turn?.reply ?? ""));
  const waiting = useWhile(turn?.doneAt ?? null, VOICE_WAIT_MS);
  // Which turn's voice has been heard: once it has, its words are not shown a second time.
  const heard = useRef<string | null>(null);
  const turnId = turn?.id ?? null;
  useEffect(() => {
    if (voice.speaking && turnId) heard.current = turnId;
  }, [voice.speaking, turnId]);

  if (hearing.phase === "listening") return { kind: "hearing", text: hearing.text, key: "hearing", typing: true };
  if (hearing.phase === "heard") return { kind: "heard", text: hearing.text, key: "heard", typing: false };
  if (turn) {
    const working: Live = {
      kind: "step",
      text: turn.steps[turn.steps.length - 1] ?? "Thinking",
      key: `step:${turn.steps.length}`,
      typing: false,
    };
    if (turn.phase === "working") return working;
    if (voice.speaking && voice.saying) {
      return { kind: "reply", text: voice.saying, key: `say:${voice.saying}`, typing: false };
    }
    if (turn.phase === "streaming") {
      // A reply that is about to be spoken is not shown early: the words would run ahead of the voice.
      const aloud = turn.spoken && app.settings.speak && app.voice.speak;
      return aloud ? working : { kind: "reply", text: turn.reply, key: `reply:${turn.id}`, typing: true };
    }
    if (!failed) {
      // Said aloud, and heard: there is nothing more to show. Said aloud, and its sound is still on the
      // way: it is not shown early.
      if (heard.current === turn.id) return hint(app.voice.hear);
      if (turn.speak === true && waiting) return working;
    }
    if (recent) {
      return failed
        ? { kind: "error", text: turn.reply, key: `error:${turn.id}`, typing: false }
        : { kind: "reply", text: turn.reply, key: `reply:${turn.id}`, typing: false };
    }
  }
  if (app.busy) return { kind: "step", text: "Working on it", key: "busy", typing: false };
  return hint(app.voice.hear);
}

const hint = (hear: boolean): Live => ({ kind: "hint", text: hear ? HINT_TALK : HINT_TYPE, key: "hint", typing: false });

export function LiveLine({ live }: { live: Live }) {
  const empty = live.kind === "hearing" && !live.text;
  const box = useRef<HTMLDivElement>(null);
  const swap = useSwap(8);
  const [overflow, setOverflow] = useState(false);
  // Short text sits at the top of its box, under the orb. Text that is longer than the box is
  // anchored to the bottom, so that the newest words stay in view while it streams in.
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
