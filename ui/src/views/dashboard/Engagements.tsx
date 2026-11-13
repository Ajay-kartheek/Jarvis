// Engagements: the people and folders of each, what Jarvis made for it, and what it remembers.
// Each remembered thing can be deleted.

import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { type Engagement, type Fact, type Item, LIBRARY_KINDS, type LibraryKind } from "../../api";
import { ago, parseTime } from "../../format";
import type { DashboardData } from "./data";
import type { Focus } from "./Library";
import { Empty, PageHead, Panel } from "./parts";

const MADE_SHOWN = 4;
const SURE_MS = 3000; // after the first press, forgetting waits this long for the second

interface Props {
  data: DashboardData;
  onOpen: (focus: Focus) => void;
}

interface Made {
  shelf: LibraryKind;
  item: Item;
}

export function Engagements({ data, onOpen }: Props) {
  const engagements = data.overview?.engagements ?? data.today?.engagements ?? [];
  const facts = data.facts ?? [];
  const known = new Set(engagements.map((engagement) => engagement.name));
  // What does not belong to a known engagement is still shown, under General.
  const general = facts.filter((fact) => !fact.engagement || !known.has(fact.engagement));

  const made = (name: string): Made[] =>
    LIBRARY_KINDS.flatMap((shelf) => (data.library?.[shelf] ?? []).map((item) => ({ shelf, item })))
      .filter(({ item }) => item.engagement === name)
      .sort((a, b) => (parseTime(b.item.at) || 0) - (parseTime(a.item.at) || 0))
      .slice(0, MADE_SHOWN);

  return (
    <>
      <PageHead title="Engagements" sub="The work Jarvis keeps apart, and what it has been told to remember about each." />
      {data.overview === null && data.today === null && <Empty>Reading…</Empty>}
      <div className="engagements">
        {engagements.map((engagement, i) => (
          <EngagementCard
            key={engagement.name}
            engagement={engagement}
            facts={facts.filter((fact) => fact.engagement === engagement.name)}
            made={made(engagement.name)}
            i={i}
            onOpen={onOpen}
            onForget={data.forget}
          />
        ))}
        {general.length > 0 && (
          <Panel i={engagements.length} title="General" className="engagement">
            <Remembered facts={general} onForget={data.forget} />
          </Panel>
        )}
      </div>
    </>
  );
}

function EngagementCard({
  engagement,
  facts,
  made,
  i,
  onOpen,
  onForget,
}: {
  engagement: Engagement;
  facts: Fact[];
  made: Made[];
  i: number;
  onOpen: Props["onOpen"];
  onForget: DashboardData["forget"];
}) {
  return (
    <Panel i={i} className="engagement">
      <header className="engagement-head">
        <h2>{engagement.name}</h2>
        <span className="stats">
          {engagement.folders} {engagement.folders === 1 ? "folder" : "folders"} · {engagement.files.toLocaleString()} files
        </span>
      </header>
      {engagement.people.length > 0 && (
        <p className="people">
          {engagement.people.map((person) => (
            <span key={person} className="chip">
              {person}
            </span>
          ))}
        </p>
      )}

      <h3 className="label">What Jarvis remembers</h3>
      <Remembered facts={facts} onForget={onForget} />

      {made.length > 0 && (
        <>
          <h3 className="label">Made for {engagement.name}</h3>
          <ul className="made">
            {made.map(({ shelf, item }) => (
              <li key={`${shelf}:${item.id}`}>
                <button className="made-item" onClick={() => onOpen({ shelf, id: item.id })}>
                  <span>{item.title}</span>
                  <time>{item.at ? ago(item.at) : ""}</time>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </Panel>
  );
}

function Remembered({ facts, onForget }: { facts: Fact[]; onForget: DashboardData["forget"] }) {
  if (facts.length === 0) return <Empty>Nothing yet. Tell Jarvis to remember something and it appears here.</Empty>;
  return (
    <ul className="facts">
      <AnimatePresence initial={false}>
        {facts.map((fact) => (
          <FactRow key={fact.id} fact={fact} onForget={onForget} />
        ))}
      </AnimatePresence>
    </ul>
  );
}

/** A deleted memory cannot be undone, so the button asks once more before it does it. */
function FactRow({ fact, onForget }: { fact: Fact; onForget: DashboardData["forget"] }) {
  const [sure, setSure] = useState(false);
  useEffect(() => {
    if (!sure) return;
    const timer = setTimeout(() => setSure(false), SURE_MS);
    return () => clearTimeout(timer);
  }, [sure]);
  return (
    <motion.li
      layout="position"
      className="fact"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, x: 12, transition: { duration: 0.18 } }}
    >
      <span>{fact.text}</span>
      <time className="quiet">{fact.at ? ago(fact.at) : ""}</time>
      <button
        className="text-btn forget"
        data-sure={sure || undefined}
        onClick={() => (sure ? void onForget(fact.id) : setSure(true))}
        aria-label={`Forget: ${fact.text}`}
      >
        {sure ? "Forget it?" : "Forget"}
      </button>
    </motion.li>
  );
}
