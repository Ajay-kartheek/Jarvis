// The conversation so far. Outside a call the newest exchange is not here: it stands under the orb,
// in full (Answer.tsx), and `History` lists what came before it, quieter and folded when long. During
// a call the orb has no room for an answer of its own, and `Trail` shows the whole of what was said to
// Jarvis since the call began, the newest exchange on top.

import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { Line } from "../api";
import { hhmm } from "../format";
import { useTallerThan } from "../hooks";
import { InlineText, Markdown } from "../markdown";
import type { Turn } from "../store";
import { Frame } from "./Frame";
import { ChevronIcon } from "./icons";

const SHOWN = 6;
const CLAMP_PX = 150; // a longer line is folded, and unfolds when clicked
const EARLIER_SHOWN = 8;
const EARLIER_CLAMP_PX = 66; // about three lines: history is for finding, and opens for reading

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

const keyOf = (line: Line) => `${line.role}:${line.at}:${line.text.length}`;

function Forget({ onForget }: { onForget: () => void }) {
  return (
    <button className="text-btn" onClick={onForget} title="Forget this conversation and start a new one">
      Forget
    </button>
  );
}

export function Trail({ lines, pending, onForget }: Props) {
  const shown = newestFirst(lines.slice(-SHOWN));
  if (shown.length === 0 && !pending) return null;
  return (
    <Frame i={1} className="trail">
      <div className="trail-head">
        <span className="label">Conversation</span>
        <Forget onForget={onForget} />
      </div>
      <ol className="trail-list">
        {pending && <Pending turn={pending} />}
        <AnimatePresence initial={false}>
          {shown.map((line, i) => (
            <motion.li
              key={keyOf(line)}
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

/**
 * What was said before the exchange under the orb, newest first. Its heading is also where the
 * conversation is forgotten, so it is there as soon as anything has been said.
 */
export function History({ lines, onForget }: { lines: Line[]; onForget: () => void }) {
  const shown = newestFirst(lines.slice(-EARLIER_SHOWN));
  return (
    <section className="earlier">
      <div className="trail-head">
        <span className="label">{shown.length > 0 ? "Earlier" : "This conversation"}</span>
        <Forget onForget={onForget} />
      </div>
      {shown.length > 0 && (
        <ol className="trail-list">
          <AnimatePresence initial={false}>
            {shown.map((line) => (
              <motion.li
                key={keyOf(line)}
                layout="position"
                className="entry"
                data-who={line.role}
                initial={{ opacity: 0, y: -8 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
              >
                <Past line={line} />
              </motion.li>
            ))}
          </AnimatePresence>
        </ol>
      )}
    </section>
  );
}

/** One earlier line: its first lines, and a way to the rest. */
function Past({ line }: { line: Line }) {
  const [open, setOpen] = useState(false);
  const [measure, tall] = useTallerThan(EARLIER_CLAMP_PX);
  return (
    <>
      <b>{line.role === "user" ? "You" : "Jarvis"}</b>
      <div className="past">
        <div
          ref={measure}
          className="entry-text"
          data-tall={tall || undefined}
          data-open={open || undefined}
          title={line.at ? hhmm(line.at) : undefined}
        >
          {line.role === "jarvis" ? (
            <div className="md">
              <Markdown text={line.text} />
            </div>
          ) : (
            <InlineText text={line.text} />
          )}
        </div>
        {tall && (
          <button className="text-btn more" onClick={() => setOpen(!open)} aria-expanded={open}>
            {open ? "Less" : "More"} <ChevronIcon open={open} />
          </button>
        )}
      </div>
    </>
  );
}
