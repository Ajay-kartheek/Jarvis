// What the panel shows while a call is being followed: the call's title and clock with its two
// buttons, the other side's words as they are spoken, the SAY card, and the call's history.

import { type CSSProperties, memo, useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { AppState, Speaker, Status } from "../api";
import { elapsed, hhmm, parseTime, shortDate } from "../format";
import { useNow, useReveal } from "../hooks";
import { InlineText } from "../markdown";
import type { HistoryItem, Suggestion } from "../store";
import { Frame } from "./Frame";
import { Stopwatch, Ticker } from "./Ticker";
import { Waveform } from "./Waveform";

const WAITING_LINE: Record<Status, string> = {
  idle: "Audio idle",
  preparing: "Getting ready…",
  listening: "Listening…",
  thinking: "Listening…",
};
const LONG_REPLY = 220; // characters; beyond this the big text steps down a size

interface BarProps {
  app: AppState;
  onSuggest: () => void;
  onStop: () => Promise<unknown>;
}

/** The call's title, how long it has run, and the two things to press. Sits beside the small orb. */
export const CallBar = memo(function CallBar({ app, onSuggest, onStop }: BarProps) {
  const now = useNow(1000);
  const [stopping, setStopping] = useState(false);
  const { meeting, status } = app;
  const started = meeting ? parseTime(meeting.started_at) : NaN;
  return (
    <motion.div className="callbar" initial={{ opacity: 0, x: 14 }} animate={{ opacity: 1, x: 0 }}>
      <div className="callbar-top">
        <motion.span
          key={status}
          className="status"
          data-tone="active"
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
        >
          {status === "idle" ? "starting" : status}
        </motion.span>
        <span className="chip">{meeting?.engagement ?? "No engagement"}</span>
        {!Number.isNaN(started) && <Ticker className="elapsed" text={elapsed(now - started)} />}
      </div>
      <h2 className="title">{meeting?.title ?? "Starting…"}</h2>
      <div className="callbar-actions">
        <button
          className="btn go"
          data-augmented-ui="tl-clip br-clip border"
          onClick={onSuggest}
          disabled={!meeting}
          title="Ask for something to say right now (Command+Enter)"
        >
          Suggest now <kbd>⌘↵</kbd>
        </button>
        <button
          className="btn ghost"
          data-augmented-ui="tl-clip br-clip border"
          disabled={stopping}
          onClick={() => {
            setStopping(true);
            void onStop().finally(() => setStopping(false));
          }}
        >
          Stop
        </button>
      </div>
    </motion.div>
  );
});

/** The sentence being spoken right now, with the level meter under it. */
export function ThemLine({ partial, status }: { partial: Record<Speaker, string>; status: Status }) {
  const speaker: Speaker = !partial.Them && partial.You ? "You" : "Them";
  const text = partial[speaker];
  return (
    <Frame i={1} className="them">
      <div className="label">{speaker} · live</div>
      <p className="them-text" data-empty={!text || undefined}>
        {text || WAITING_LINE[status]}
      </p>
      <Waveform />
    </Frame>
  );
}

/**
 * Splits a reply into what to say and the optional "Also:" aside. While text is still streaming,
 * a last line that is only part-way through spelling "Also:" is held back so the big text does not jump.
 */
function splitAlso(text: string, streaming: boolean): [main: string, also: string] {
  const lines = text.split("\n");
  const at = lines.findIndex((line) => /^\s*also:/i.test(line));
  if (at > 0) {
    const also = lines
      .slice(at)
      .join("\n")
      .replace(/^\s*also:\s*/i, "");
    return [lines.slice(0, at).join("\n").trim(), also.trim()];
  }
  if (streaming && lines.length > 1 && /^\s*a(l(so?)?)?$/i.test(lines[lines.length - 1])) lines.pop();
  return [lines.join("\n").trim(), ""];
}

export const SayCard = memo(function SayCard({ item }: { item: Suggestion | null }) {
  const phase = item?.phase ?? "empty";
  const [main, also] = splitAlso(item?.text ?? "", phase === "streaming");
  const total = main.length + also.length;
  const shown = useReveal(total, item?.id, { instant: phase === "done" });
  const pending = phase === "thinking" || phase === "streaming";
  const typing = phase === "streaming" || shown < total;
  // Age of this reply as a negative animation delay, fixed when the reply first appears: the amber
  // "new" flag then fades on schedule even if the screen is left and reopened.
  const age = useMemo(() => `${((item?.since ?? Date.now()) - Date.now()) / 1000}s`, [item?.id]);

  return (
    <Frame i={2} className="say">
      {/* Keyed by suggestion so each new one slides in. */}
      <motion.div
        key={item?.id ?? "none"}
        className="say-card"
        data-phase={phase}
        style={{ "--age": age } as CSSProperties}
        initial={{ opacity: 0, y: 14 }}
        animate={{ opacity: 1, y: 0 }}
      >
        <div className="say-head">
          <span className="tag">Say</span>
          <span className="question">{item?.question}</span>
          {item && pending && <Stopwatch since={item.since} />}
          {item && !pending && item.seconds != null && <span className="took">{item.seconds.toFixed(1)}s</span>}
        </div>
        {phase === "empty" && <p className="quiet">Standing by. Replies appear here during a call.</p>}
        {phase === "skipped" && <p className="quiet">No reply needed.</p>}
        {phase === "thinking" && <div className="thinking">Working on it</div>}
        {total > 0 && (
          <p
            className="say-text"
            data-long={main.length > LONG_REPLY || undefined}
            data-typing={(typing && shown <= main.length) || undefined}
          >
            <InlineText text={main} limit={shown} />
          </p>
        )}
        {shown > main.length && (
          <p className="also" data-typing={typing || undefined}>
            <span className="tag">Also</span>
            <InlineText text={also} limit={shown - main.length} />
          </p>
        )}
        {phase === "done" && item && item.sources.length > 0 && (
          <ul className="sources">
            {item.sources.map((source, i) => (
              <motion.li
                key={i}
                className="chip"
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.15 + i * 0.08 }}
              >
                <b>{source.kind}</b>
                {source.title}
                {/\d/.test(source.date) && <time>{shortDate(source.date)}</time>}
              </motion.li>
            ))}
          </ul>
        )}
      </motion.div>
    </Frame>
  );
});

/** Earlier replies and transcript lines of this call, newest first. */
export const CallHistory = memo(function CallHistory({ items }: { items: HistoryItem[] }) {
  if (items.length === 0) return null;
  return (
    <Frame i={3} className="history">
      <div className="label">This call</div>
      <ol className="history-list">
        <AnimatePresence initial={false}>
          {items.map((entry) => (
            <motion.li key={entry.key} initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}>
              {entry.kind === "line" ? (
                <div className="entry" data-who={entry.speaker.toLowerCase()}>
                  <time>{hhmm(entry.at)}</time>
                  <b>{entry.speaker}</b>
                  <span>{entry.text}</span>
                </div>
              ) : (
                <PastReply item={entry.item} />
              )}
            </motion.li>
          ))}
        </AnimatePresence>
      </ol>
    </Frame>
  );
});

function PastReply({ item }: { item: Suggestion }) {
  const [main, also] = splitAlso(item.text, false);
  return (
    <div className="entry" data-who="say">
      <time>{item.at ? hhmm(item.at) : ""}</time>
      <b>Say</b>
      <span>
        <InlineText text={main} />
        {also && (
          <small>
            Also: <InlineText text={also} />
          </small>
        )}
      </span>
    </div>
  );
}
