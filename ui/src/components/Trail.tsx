// The last few turns of the conversation. The newest exchange is on top, right under the orb, so the
// answer just given is readable without scrolling however small the panel is; older ones fade below it.

import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { Line } from "../api";
import { hhmm } from "../format";
import { useTallerThan } from "../hooks";
import { InlineText } from "../markdown";
import type { Turn } from "../store";
import { Frame } from "./Frame";

const SHOWN = 6;
const CLAMP_PX = 150; // a longer line is folded, and unfolds when clicked

interface Props {
  lines: Line[];
  /** During a call the live line belongs to the call, so a turn in progress is shown here instead. */
  pending: Turn | null;
  onForget: () => void;
}

/** Newest exchange first; inside an exchange the question still comes before its answer. */
function newestFirst(lines: Line[]): Line[] {
  const exchanges: Line[][] = [];
  for (const line of lines) {
    if (line.role === "user" || exchanges.length === 0) exchanges.push([line]);
    else exchanges[exchanges.length - 1].push(line);
  }
  return exchanges.reverse().flat();
}

export function Trail({ lines, pending, onForget }: Props) {
  const shown = newestFirst(lines.slice(-SHOWN));
  if (shown.length === 0 && !pending) return null;
  return (
    <Frame i={1} className="trail">
      <div className="trail-head">
        <span className="label">Conversation</span>
        <button className="text-btn" onClick={onForget} title="Forget this conversation and start a new one">
          Forget
        </button>
      </div>
      <ol className="trail-list">
        {pending && <Pending turn={pending} />}
        <AnimatePresence initial={false}>
          {shown.map((line, i) => (
            <motion.li
              key={`${line.role}:${line.at}:${line.text.length}`}
              layout="position"
              className="entry"
              data-who={line.role}
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1 - (0.45 * i) / shown.length, y: 0 }}
              exit={{ opacity: 0 }}
            >
              <Entry line={line} newest={i < 2} />
            </motion.li>
          ))}
        </AnimatePresence>
      </ol>
    </Frame>
  );
}

/** The newest exchange is never folded: it is what the user is reading. */
function Entry({ line, newest }: { line: Line; newest: boolean }) {
  const [open, setOpen] = useState(newest);
  const [measure, tall] = useTallerThan(CLAMP_PX);
  return (
    <>
      <b>{line.role === "user" ? "You" : "Jarvis"}</b>
      <div
        ref={measure}
        className="entry-text"
        data-tall={tall || undefined}
        data-open={open || undefined}
        onClick={tall ? () => setOpen(!open) : undefined}
        title={line.at ? hhmm(line.at) : undefined}
      >
        <InlineText text={line.text} />
      </div>
    </>
  );
}

/** What Jarvis is doing about the latest message: its latest step, or the reply as it comes in. */
function Pending({ turn }: { turn: Turn }) {
  const step = turn.steps[turn.steps.length - 1];
  return (
    <li className="entry" data-who="jarvis">
      <b>Jarvis</b>
      {turn.phase === "streaming" ? (
        <div className="entry-text" data-typing>
          <InlineText text={turn.reply} />
        </div>
      ) : (
        <div className="entry-text thinking">{step ?? "Thinking"}</div>
      )}
    </li>
  );
}
