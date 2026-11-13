// Activity: what Jarvis did, newest first, a day at a time.

import { useState } from "react";
import { motion } from "motion/react";
import type { Activity as Entry } from "../../api";
import { dayKey, dayLabel, hhmm } from "../../format";
import type { DashboardData } from "./data";
import { Empty, PageHead } from "./parts";

const ANIMATED = 24; // only the first rows slide in; a long list would take for ever to settle

export function Activity({ data }: { data: DashboardData }) {
  const [kind, setKind] = useState<string | null>(null);
  const all = data.activity;
  const kinds = [...new Set((all ?? []).map((entry) => entry.kind))].sort();
  const shown = (all ?? []).filter((entry) => kind === null || entry.kind === kind);
  const days = groupByDay(shown);

  return (
    <>
      <PageHead title="Activity" sub="What Jarvis did, and when." />

      {kinds.length > 1 && (
        <div className="filters" role="group" aria-label="Kind of activity">
          <button className="filter" aria-pressed={kind === null} onClick={() => setKind(null)}>
            All
          </button>
          {kinds.map((name) => (
            <button
              key={name}
              className="filter"
              aria-pressed={kind === name}
              onClick={() => setKind(kind === name ? null : name)}
            >
              {name}
            </button>
          ))}
        </div>
      )}

      {all === null && <Empty>Reading…</Empty>}
      {all !== null && shown.length === 0 && <Empty>Nothing yet.</Empty>}

      <div className="days">
        {days.map(({ key, label, entries }) => (
          <section key={key} className="day">
            <h2 className="label">{label}</h2>
            <ol className="log">
              {entries.map((entry, i) => (
                <motion.li
                  key={`${entry.at}:${entry.text}`}
                  initial={i < ANIMATED ? { opacity: 0, x: -8 } : false}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: Math.min(i, ANIMATED) * 0.02 }}
                >
                  <time>{hhmm(entry.at)}</time>
                  <span className="chip" data-kind={entry.kind}>
                    {entry.kind}
                  </span>
                  <span>{entry.text}</span>
                </motion.li>
              ))}
            </ol>
          </section>
        ))}
      </div>
    </>
  );
}

function groupByDay(entries: Entry[]) {
  const days: { key: string; label: string; entries: Entry[] }[] = [];
  for (const entry of entries) {
    const key = dayKey(entry.at);
    const last = days[days.length - 1];
    if (last?.key === key) last.entries.push(entry);
    else days.push({ key, label: dayLabel(entry.at), entries: [entry] });
  }
  return days;
}
