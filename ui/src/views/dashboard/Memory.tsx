// Memory: what Jarvis knows about your words and facts. What it was told to remember, by engagement,
// with a way to add something; the words it listens for, the ones you taught it and the ones it found
// itself; and how names are said when it speaks. The words and the names show when the service has them.

import { type FormEvent, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { Pronunciation, Vocabulary, WordSource } from "../../api";
import { CloseIcon } from "../../components/icons";
import type { DashboardData } from "./data";
import { Remembered } from "./Engagements";
import { Empty, PageHead, Panel } from "./parts";

// What the service takes, so that a button is not offered for what it would refuse. It checks again.
const FACT_MIN = 4;
const FACT_MAX = 600;
const WORD_MIN = 2;
const NAME_MAX = 60; // a word, and each side of a name

const SOURCES: { source: WordSource; label: string }[] = [
  { source: "engagement", label: "Engagements" },
  { source: "person", label: "People" },
  { source: "dossier", label: "Dossier terms" },
  { source: "common", label: "Common terms" },
];

/** Text as the service keeps it: trimmed, and every run of spaces or line breaks one space. */
const tidy = (text: string) => text.trim().replace(/\s+/g, " ");

/**
 * What a form does when it is sent: it waits for the server, and clears itself only if the server took
 * what was sent. A refusal is already shown as a warning, and the text stays to be changed.
 */
function useSend(ready: boolean, send: () => Promise<boolean>, clear: () => void) {
  const [busy, setBusy] = useState(false);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    void send()
      .then((taken) => {
        if (taken) clear();
      })
      .finally(() => setBusy(false));
  };
  return { disabled: busy || !ready, submit };
}

export function Memory({ data }: { data: DashboardData }) {
  return (
    <>
      <PageHead title="Memory" sub="What Jarvis remembers, the words it listens for, and how it says names." />
      <Facts data={data} />
      {data.vocabulary && <Words vocabulary={data.vocabulary} onTeach={data.teach} onUnteach={data.unteach} />}
      {data.pronunciations && (
        <Names pairs={data.pronunciations} onPronounce={data.pronounce} onUnpronounce={data.unpronounce} />
      )}
    </>
  );
}

// ---------- Remembered ----------

function Facts({ data }: { data: DashboardData }) {
  const { facts } = data;
  const engagements = (data.overview?.engagements ?? data.today?.engagements ?? []).map(({ name }) => name);
  const known = new Set(engagements);
  // What does not belong to a known engagement is still shown, under General.
  const groups = [
    ...engagements.map((name) => ({ name, facts: (facts ?? []).filter((fact) => fact.engagement === name) })),
    { name: "General", facts: (facts ?? []).filter((fact) => !fact.engagement || !known.has(fact.engagement)) },
  ].filter((group) => group.facts.length > 0);

  return (
    <Panel
      i={0}
      title="Remembered"
      aside={facts && `${facts.length} ${facts.length === 1 ? "fact" : "facts"}`}
      className="memory"
    >
      <Remember engagements={engagements} onRemember={data.remember} />
      {facts === null && <Empty>Reading…</Empty>}
      {facts?.length === 0 && <Empty>Nothing yet. Tell Jarvis to remember something, or add it here.</Empty>}
      {groups.map((group) => (
        <section key={group.name} className="group">
          <h3 className="label">{group.name}</h3>
          <Remembered facts={group.facts} onForget={data.forget} />
        </section>
      ))}
    </Panel>
  );
}

function Remember({ engagements, onRemember }: { engagements: string[]; onRemember: DashboardData["remember"] }) {
  const [text, setText] = useState("");
  const [engagement, setEngagement] = useState("");
  const fact = tidy(text);
  const { disabled, submit } = useSend(
    fact.length >= FACT_MIN,
    () => onRemember(fact, engagement || null),
    () => setText(""),
  );
  return (
    <form className="add" onSubmit={submit}>
      <input
        className="search"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="Something for Jarvis to remember"
        aria-label="A fact to remember"
        maxLength={FACT_MAX}
        autoComplete="off"
      />
      <select
        className="select"
        value={engagement}
        onChange={(event) => setEngagement(event.target.value)}
        aria-label="Engagement"
      >
        <option value="">General</option>
        {engagements.map((name) => (
          <option key={name}>{name}</option>
        ))}
      </select>
      <button className="btn" data-augmented-ui="tl-clip br-clip border" disabled={disabled}>
        Remember
      </button>
    </form>
  );
}

// ---------- Words Jarvis listens for ----------

interface WordsProps {
  vocabulary: Vocabulary;
  onTeach: DashboardData["teach"];
  onUnteach: DashboardData["unteach"];
}

function Words({ vocabulary, onTeach, onUnteach }: WordsProps) {
  const { taught, automatic } = vocabulary;
  return (
    <Panel
      i={1}
      title="Words Jarvis listens for"
      aside={`${taught.length} added · ${automatic.length} found`}
      className="memory"
    >
      <p className="sub">Jarvis leans towards these when it hears you, so names and terms come out as you write them.</p>
      <AddWord onTeach={onTeach} />

      <section className="group">
        <h3 className="label">Added by you</h3>
        {taught.length === 0 ? (
          <Empty>None yet. Tell Jarvis a word it got wrong, or add one here.</Empty>
        ) : (
          <ul className="words">
            <AnimatePresence initial={false}>
              {[...taught].reverse().map((word) => (
                <motion.li
                  key={word}
                  layout="position"
                  initial={{ opacity: 0, scale: 0.92 }}
                  animate={{ opacity: 1, scale: 1 }}
                  exit={{ opacity: 0, scale: 0.92, transition: { duration: 0.15 } }}
                >
                  <span className="chip taught">
                    <span title={word}>{word}</span>
                    <button
                      className="icon small"
                      onClick={() => void onUnteach(word)}
                      title="Stop listening for it"
                      aria-label={`Remove ${word}`}
                    >
                      <CloseIcon />
                    </button>
                  </span>
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        )}
      </section>

      <section className="group">
        <h3 className="label">Found by Jarvis</h3>
        <dl className="found">
          {SOURCES.map(({ source, label }) => {
            const words = automatic.filter((heard) => heard.source === source);
            if (words.length === 0) return null;
            return (
              <div key={source}>
                <dt>{label}</dt>
                <dd className="words">
                  {words.map(({ word }) => (
                    <span key={word} className="chip">
                      {word}
                    </span>
                  ))}
                </dd>
              </div>
            );
          })}
        </dl>
      </section>
    </Panel>
  );
}

function AddWord({ onTeach }: { onTeach: DashboardData["teach"] }) {
  const [text, setText] = useState("");
  const word = tidy(text);
  const { disabled, submit } = useSend(word.length >= WORD_MIN, () => onTeach(word), () => setText(""));
  return (
    <form className="add" onSubmit={submit}>
      <input
        className="search"
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="A name or term"
        aria-label="A word or name to listen for"
        maxLength={NAME_MAX}
        autoComplete="off"
        spellCheck={false}
      />
      <button className="btn" data-augmented-ui="tl-clip br-clip border" disabled={disabled}>
        Add
      </button>
    </form>
  );
}

// ---------- How names are said ----------

interface NamesProps {
  pairs: Pronunciation[];
  onPronounce: DashboardData["pronounce"];
  onUnpronounce: DashboardData["unpronounce"];
}

function Names({ pairs, onPronounce, onUnpronounce }: NamesProps) {
  return (
    <Panel
      i={2}
      title="How names are said"
      aside={`${pairs.length} ${pairs.length === 1 ? "name" : "names"}`}
      className="memory"
    >
      <p className="sub">Spell a name the way it should sound, and Jarvis says it that way. What it writes stays as it is.</p>
      <AddName onPronounce={onPronounce} />
      {pairs.length === 0 ? (
        <Empty>None yet. Add a name that Jarvis says wrong.</Empty>
      ) : (
        <ul className="names">
          <AnimatePresence initial={false}>
            {[...pairs].reverse().map((pair) => (
              <NameRow key={pair.written} pair={pair} onRemove={onUnpronounce} />
            ))}
          </AnimatePresence>
        </ul>
      )}
    </Panel>
  );
}

function NameRow({ pair, onRemove }: { pair: Pronunciation; onRemove: DashboardData["unpronounce"] }) {
  return (
    <motion.li
      layout="position"
      className="name"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, x: 12, transition: { duration: 0.18 } }}
    >
      <span>{pair.written}</span>
      <span className="arrow" role="img" aria-label="is said as">
        →
      </span>
      <span className="spelt">{pair.said}</span>
      <button
        className="text-btn forget"
        onClick={() => void onRemove(pair.written)}
        aria-label={`Remove how ${pair.written} is said`}
      >
        Remove
      </button>
    </motion.li>
  );
}

function AddName({ onPronounce }: { onPronounce: DashboardData["pronounce"] }) {
  const [written, setWritten] = useState("");
  const [said, setSaid] = useState("");
  const name = tidy(written);
  const spelt = tidy(said);
  const { disabled, submit } = useSend(
    name !== "" && spelt !== "",
    () => onPronounce(name, spelt),
    () => {
      setWritten("");
      setSaid("");
    },
  );
  return (
    <form className="add" onSubmit={submit}>
      <input
        className="search"
        value={written}
        onChange={(event) => setWritten(event.target.value)}
        placeholder="Written as"
        aria-label="The name as it is written"
        maxLength={NAME_MAX}
        autoComplete="off"
        spellCheck={false}
      />
      <span className="arrow" aria-hidden="true">
        →
      </span>
      <input
        className="search"
        value={said}
        onChange={(event) => setSaid(event.target.value)}
        placeholder="Said as"
        aria-label="The name spelt as it should be said"
        maxLength={NAME_MAX}
        autoComplete="off"
        spellCheck={false}
      />
      <button className="btn" data-augmented-ui="tl-clip br-clip border" disabled={disabled}>
        Add
      </button>
    </form>
  );
}
