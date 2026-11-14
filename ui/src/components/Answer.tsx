// What Jarvis said last, in full, right under the orb. It is the main thing on the panel for as long
// as it is the latest thing said: large, never folded, never cut short. A long answer scrolls with
// the feed it sits at the top of. Above it, small, the message it answers. Older exchanges are the
// history further down (see Trail.tsx).

import { useState } from "react";
import { motion } from "motion/react";
import { Markdown } from "../markdown";
import type { Model } from "../store";

export interface Exchange {
  /** Changes when another exchange takes this place. */
  key: string;
  /** The user's message; empty when Jarvis spoke up by itself. */
  question: string;
  answer: string;
  phase: "working" | "streaming" | "done" | "error";
  /** How many lines at the end of the conversation this shows. The history lists the ones before. */
  covers: number;
}

// An answer of one paragraph, up to this many characters, is set like a spoken line: centred under the orb.
// Most spoken answers stay below it from their first word to their last, so they do not jump as they come in.
const SHORT = 200;

/**
 * The exchange to show under the orb. While Jarvis is working on a message, the turn knows more than
 * the conversation does; once it has answered, the conversation is what the server keeps too, so
 * the same answer is there after the window is reloaded.
 */
export function latestExchange({ app, turn }: Model): Exchange | null {
  const lines = app.conversation;
  const last = lines.length ? lines[lines.length - 1] : null;
  const asked = turn !== null && last?.role === "user" && last.text === turn.text;
  if (turn && (turn.phase === "working" || turn.phase === "streaming")) {
    return { key: turn.id, question: turn.text, answer: turn.reply, phase: turn.phase, covers: asked ? 1 : 0 };
  }
  // A failed turn leaves no line of its own: its reason stands until something else is said.
  if (turn?.phase === "error" && (asked || !last)) {
    return { key: turn.id, question: turn.text, answer: turn.reply, phase: "error", covers: asked ? 1 : 0 };
  }
  if (!last) return null;
  if (last.role === "jarvis") {
    const mine = turn?.phase === "done" && turn.reply === last.text ? turn : null;
    const before = lines.length > 1 ? lines[lines.length - 2] : null;
    // The turn knows what it answers (nothing, when Jarvis spoke up by itself). Without one, the line before does.
    const question = mine ? mine.text : before?.role === "user" ? before.text : "";
    const both = question !== "" && before?.role === "user" && before.text === question;
    return { key: mine?.id ?? `${last.at}:${last.text.length}`, question, answer: last.text, phase: "done", covers: both ? 2 : 1 };
  }
  // The newest line is the user's and no turn of this window is answering it: another window asked,
  // or this one was opened in the middle of it.
  return app.busy ? { key: `${last.at}:asked`, question: last.text, answer: "", phase: "working", covers: 1 } : null;
}

export function Answer({ exchange }: { exchange: Exchange }) {
  const { question, answer, phase } = exchange;
  const [whole, setWhole] = useState(false);
  const short = answer.length <= SHORT && !answer.trim().includes("\n");
  return (
    <motion.section
      className="latest"
      data-phase={phase}
      data-short={short || undefined}
      aria-live={phase === "streaming" ? "off" : "polite"}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
    >
      {question && (
        // Two lines of it are enough to know what is being answered; a click shows the rest.
        <p className="asked" data-whole={whole || undefined} onClick={() => setWhole(!whole)} title="What you said">
          <b>You</b>
          {question}
        </p>
      )}
      {answer && (
        <div className="said md" data-typing={phase === "streaming" || undefined}>
          <Markdown text={answer} />
        </div>
      )}
    </motion.section>
  );
}
