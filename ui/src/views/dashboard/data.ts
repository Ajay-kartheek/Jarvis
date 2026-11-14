// What the dashboard reads from the server, kept fresh: again when Jarvis does or makes something,
// and every few minutes in case a message was missed. Reminders, promises, dossiers, and the words and
// names of the Memory page are parts the service may not have yet: one it does not have reads as null,
// and its place stays empty.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type Activity,
  type Api,
  type Dossier,
  type Engagement,
  type Fact,
  type Library,
  type Overview,
  type PromiseEntry,
  type Pronunciation,
  type Reminder,
  type Today,
  type Vocabulary,
  optional,
} from "../../api";

const REFRESH_MS = 3 * 60_000;
const SETTLE_MS = 700; // events come in bursts; one read after each burst is enough
const ACTIVITY_ROWS = 200;

interface Loaded {
  overview: Overview | null;
  today: Today | null;
  library: Library | null;
  facts: Fact[] | null;
  activity: Activity[] | null;
  /** Null: not read yet, or the service has no such thing. Either way there is nothing to show. */
  reminders: Reminder[] | null;
  promises: PromiseEntry[] | null;
  /** By engagement name; an engagement the service has no dossier for is not in it. */
  dossiers: Record<string, Dossier> | null;
  /** The words Jarvis listens for, and how names are said: null as well when the service has no such thing. */
  vocabulary: Vocabulary | null;
  pronunciations: Pronunciation[] | null;
}

export interface DashboardData extends Loaded {
  /** The reason the last read failed, or empty. What was read before stays on screen. */
  error: string;
  /** Read everything again now. */
  refresh: () => void;
  /** Delete one thing Jarvis was told to remember. It disappears at once; it comes back if the server refuses. */
  forget: (id: Fact["id"]) => Promise<void>;
  /** Keep a fact that was typed, for an engagement or (null) in general. False if the server refused: the reason shows as a warning. */
  remember: (text: string, engagement: Fact["engagement"]) => Promise<boolean>;
  /** Add a word to those Jarvis listens for. False if the server refused: the reason shows as a warning. */
  teach: (word: string) => Promise<boolean>;
  /** Take a word the user taught off the list. It goes at once; it comes back if the server refuses. */
  unteach: (word: string) => Promise<void>;
  /** Say how a name is to be said. False if the server refused: the reason shows as a warning. */
  pronounce: (written: string, said: string) => Promise<boolean>;
  /** Drop a name from those said in a special way. It goes at once; it comes back if the server refuses. */
  unpronounce: (written: string) => Promise<void>;
}

const NOTHING: Loaded = {
  overview: null,
  today: null,
  library: null,
  facts: null,
  activity: null,
  reminders: null,
  promises: null,
  dossiers: null,
  vocabulary: null,
  pronunciations: null,
};
const reason = (error: unknown) => (error instanceof Error ? error.message : String(error));

/** The dossier of each engagement that has one. */
async function readDossiers(api: Api, engagements: Engagement[]): Promise<Record<string, Dossier>> {
  const found = await Promise.all(engagements.map(({ name }) => optional(api.engagement(name))));
  const dossiers: Record<string, Dossier> = {};
  engagements.forEach(({ name }, i) => {
    const dossier = found[i];
    if (dossier) dossiers[name] = dossier;
  });
  return dossiers;
}

/**
 * `fresh` goes up whenever the page hears of a task, card, note, brief or state change, `made` when
 * something was made or dropped. A task's every step changes `fresh` but not the library, so the
 * library, the brief, the dossiers, what is remembered, the words and the names are read again only
 * when `made` moved, or every few minutes; the overview, the activity, the reminders and the promises
 * are read each time.
 */
export function useDashboardData(api: Api, signal: { fresh: number; made: number }, warn: (text: string) => void): DashboardData {
  const [data, setData] = useState<Loaded>(NOTHING);
  const [error, setError] = useState("");
  const [round, setRound] = useState(0);
  const latest = useRef(signal);
  const seen = useRef(signal);
  const wanted = useRef(1); // goes up whenever the next read must take everything
  const done = useRef(0); // the value of `wanted` that the last complete read answered
  const waiting = useRef(0);
  latest.current = signal;

  useEffect(() => {
    if (signal.fresh === seen.current.fresh && signal.made === seen.current.made) return;
    if (waiting.current) return; // a read is already due; it will see this too
    waiting.current = window.setTimeout(() => {
      waiting.current = 0;
      if (latest.current.made !== seen.current.made) wanted.current += 1;
      seen.current = latest.current;
      setRound((n) => n + 1);
    }, SETTLE_MS);
  }, [signal.fresh, signal.made]);
  useEffect(() => {
    const timer = setInterval(() => {
      wanted.current += 1;
      setRound((n) => n + 1);
    }, REFRESH_MS);
    return () => {
      clearInterval(timer);
      clearTimeout(waiting.current);
      waiting.current = 0;
    };
  }, []);
  useEffect(() => {
    done.current = 0; // another server: nothing read from the last one counts
  }, [api]);

  useEffect(() => {
    let current = true;
    const asked = wanted.current;
    const all = asked !== done.current;
    const read = <T>(job: () => Promise<T>, ask: boolean) => (ask ? job() : Promise.resolve(undefined));
    const overviewRead = api.overview();
    Promise.allSettled([
      overviewRead,
      read(() => api.today(), all),
      read(() => api.library(), all),
      read(() => api.facts(), all),
      api.activity(ACTIVITY_ROWS),
      optional(api.reminders()),
      optional(api.promises()),
      // The overview says which engagements there are. If it fails, it is the one to report: not this.
      read(() => overviewRead.then(({ engagements }) => readDossiers(api, engagements), () => undefined), all),
      read(() => optional(api.vocabulary()), all),
      read(() => optional(api.pronunciations()), all),
    ]).then((results) => {
      if (!current) return;
      const [overview, today, library, facts, activity, reminders, promises, dossiers, vocabulary, pronunciations] = results;
      // One that failed keeps what it had, and so does one that was not asked for; the others are shown.
      const take = <T,>(result: PromiseSettledResult<T | undefined>, old: T | null) =>
        result.status === "fulfilled" && result.value !== undefined ? result.value : old;
      setData((old) => ({
        overview: take(overview, old.overview),
        today: take(today, old.today),
        library: take(library, old.library),
        facts: take(facts, old.facts),
        activity: take(activity, old.activity),
        reminders: take(reminders, old.reminders),
        promises: take(promises, old.promises),
        dossiers: take(dossiers, old.dossiers),
        vocabulary: take(vocabulary, old.vocabulary),
        pronunciations: take(pronunciations, old.pronunciations),
      }));
      const failed = results.find((result): result is PromiseRejectedResult => result.status === "rejected");
      setError(failed ? reason(failed.reason) : "");
      if (all && !failed) done.current = asked;
    });
    return () => {
      current = false;
    };
  }, [api, round]);

  const refresh = useCallback(() => {
    wanted.current += 1;
    setRound((n) => n + 1);
  }, []);

  const forget = useCallback(
    async (id: Fact["id"]) => {
      setData((old) => ({ ...old, facts: old.facts?.filter((fact) => fact.id !== id) ?? null }));
      try {
        await api.forgetFact(id);
      } catch (failure) {
        warn(reason(failure));
        wanted.current += 1;
        setRound((n) => n + 1);
      }
    },
    [api, warn],
  );

  /** Runs a change and applies what comes back; a refusal is shown as a warning. */
  const change = useCallback(
    async <T,>(job: Promise<T>, apply: (result: T) => void): Promise<boolean> => {
      try {
        apply(await job);
        return true;
      } catch (failure) {
        warn(reason(failure));
        return false;
      }
    },
    [warn],
  );

  const remember = useCallback(
    (text: string, engagement: Fact["engagement"]) =>
      change(api.addFact(text, engagement), (fact) =>
        setData((old) => ({ ...old, facts: [fact, ...(old.facts ?? []).filter((kept) => kept.id !== fact.id)] })),
      ),
    [api, change],
  );

  const teach = useCallback(
    (word: string) => change(api.teachWord(word), (vocabulary) => setData((old) => ({ ...old, vocabulary }))),
    [api, change],
  );

  const unteach = useCallback(
    async (word: string) => {
      setData((old) => ({
        ...old,
        vocabulary: old.vocabulary && { ...old.vocabulary, taught: old.vocabulary.taught.filter((taught) => taught !== word) },
      }));
      const changed = await change(api.unteachWord(word), (vocabulary) => setData((old) => ({ ...old, vocabulary })));
      if (!changed) refresh();
    },
    [api, change, refresh],
  );

  const pronounce = useCallback(
    (written: string, said: string) =>
      change(api.addPronunciation(written, said), (pronunciations) => setData((old) => ({ ...old, pronunciations }))),
    [api, change],
  );

  const unpronounce = useCallback(
    async (written: string) => {
      setData((old) => ({ ...old, pronunciations: old.pronunciations?.filter((pair) => pair.written !== written) ?? null }));
      const changed = await change(api.removePronunciation(written), (pronunciations) =>
        setData((old) => ({ ...old, pronunciations })),
      );
      if (!changed) refresh();
    },
    [api, change, refresh],
  );

  return { ...data, error, refresh, forget, remember, teach, unteach, pronounce, unpronounce };
}
