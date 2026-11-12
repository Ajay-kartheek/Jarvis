import { type CSSProperties, memo, useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { AppState, Speaker, Status } from "../api";
import type { HistoryItem, Model, Suggestion } from "../store";
import { Frame } from "../components/Frame";
import { Stopwatch, Ticker } from "../components/Ticker";
import { Waveform } from "../components/Waveform";
import { clock, elapsed, hhmm, parseTime, shortDate } from "../format";
import { useNow, useReveal } from "../hooks";
import { InlineText } from "../markdown";

interface Props {
  model: Model;
  onStart: (project: string | null, headphones: boolean) => Promise<void>;
  onStop: () => Promise<void>;
  onSuggest: () => void;
}

const CLIPPED = "tl-clip br-clip border";
const DROP_IN = { initial: { opacity: 0, y: -6 }, animate: { opacity: 1, y: 0 } };
const WAITING_LINE: Record<Status, string> = {
  idle: "Audio idle",
  preparing: "Getting ready…",
  listening: "Listening…",
  thinking: "Listening…",
};
const LONG_REPLY = 220; // characters; beyond this the big text steps down a size

export function Meeting({ model, onStart, onStop, onSuggest }: Props) {
  const { app, partial, current, history } = model;
  return (
    <div className="meeting">
      <TopBar app={app} onStart={onStart} onStop={onStop} />
      <LiveLine partial={partial} status={app.status} />
      <SayCard item={current} />
      <History items={history} />
      <button className="btn go" data-augmented-ui={CLIPPED} onClick={onSuggest} disabled={!app.meeting}>
        Suggest now <kbd>⌘↵</kbd>
      </button>
    </div>
  );
}

const TopBar = memo(function TopBar({ app, onStart, onStop }: { app: AppState } & Pick<Props, "onStart" | "onStop">) {
  const now = useNow(1000);
  const [project, setProject] = useState("");
  const [headphones, setHeadphones] = useState(false);
  const [busy, setBusy] = useState(false);
  const { status, meeting, engagements } = app;
  const active = meeting !== null || status !== "idle"; // "preparing" arrives before the meeting's details do
  const chosen = engagements.some((e) => e.name === project) ? project : "";
  const startedAt = meeting ? parseTime(meeting.started_at) : NaN;

  const act = (request: Promise<void>) => {
    setBusy(true);
    void request.finally(() => setBusy(false));
  };

  return (
    <Frame i={0} className="topbar">
      <div className="row">
        <Ticker className="clock" text={clock(now)} />
        <h1 className="title">{meeting?.title ?? (active ? "Starting…" : "No meeting")}</h1>
        <motion.span key={status} className="status" data-status={status} {...DROP_IN}>
          {status}
        </motion.span>
      </div>
      {active ? (
        <motion.div key="running" className="row" {...DROP_IN}>
          <span className="chip">{(meeting ? meeting.engagement : chosen) || "No engagement"}</span>
          {!Number.isNaN(startedAt) && <Ticker className="elapsed" text={elapsed(now - startedAt)} />}
          <button className="btn" data-augmented-ui={CLIPPED} onClick={() => act(onStop())} disabled={busy}>
            Stop
          </button>
        </motion.div>
      ) : (
        <motion.div key="setup" className="row" {...DROP_IN}>
          <select
            className="select"
            aria-label="Engagement"
            value={chosen}
            onChange={(e) => setProject(e.target.value)}
          >
            <option value="">No engagement</option>
            {engagements.map((e) => (
              <option key={e.name}>{e.name}</option>
            ))}
          </select>
          <label className="toggle">
            <input type="checkbox" checked={headphones} onChange={(e) => setHeadphones(e.target.checked)} />
            Headphones
          </label>
          <button
            className="btn"
            data-augmented-ui={CLIPPED}
            onClick={() => act(onStart(chosen || null, headphones))}
            disabled={busy}
          >
            Start
          </button>
        </motion.div>
      )}
    </Frame>
  );
});

/** The sentence being spoken right now, with the level meter under it. */
function LiveLine({ partial, status }: { partial: Record<Speaker, string>; status: Status }) {
  const speaker: Speaker = !partial.Them && partial.You ? "You" : "Them";
  const text = partial[speaker];
  return (
    <Frame i={1} className="live">
      <div className="label">{speaker} · live</div>
      <p className="live-text" data-empty={!text || undefined}>
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

const SayCard = memo(function SayCard({ item }: { item: Suggestion | null }) {
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
        {phase === "empty" && <p className="quiet">Standing by. Replies appear here during a meeting.</p>}
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

/** Earlier replies and transcript lines, newest first. */
const History = memo(function History({ items }: { items: HistoryItem[] }) {
  return (
    <Frame i={3} className="history">
      <div className="label">History</div>
      {items.length === 0 && <p className="quiet">Nothing yet.</p>}
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
