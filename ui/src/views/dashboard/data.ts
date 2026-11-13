// What the dashboard reads from the server, kept fresh: again when Jarvis does or makes something,
// and every few minutes in case a message was missed.

import { useCallback, useEffect, useRef, useState } from "react";
import type { Activity, Api, Fact, Library, Overview, Today } from "../../api";

const REFRESH_MS = 3 * 60_000;
const SETTLE_MS = 700; // events come in bursts; one read after each burst is enough
const ACTIVITY_ROWS = 200;

interface Loaded {
  overview: Overview | null;
  today: Today | null;
  library: Library | null;
  facts: Fact[] | null;
  activity: Activity[] | null;
}

export interface DashboardData extends Loaded {
  /** The reason the last read failed, or empty. What was read before stays on screen. */
  error: string;
  /** Read everything again now. */
  refresh: () => void;
  /** Delete one thing Jarvis was told to remember. It disappears at once; it comes back if the server refuses. */
  forget: (id: Fact["id"]) => Promise<void>;
}

const NOTHING: Loaded = { overview: null, today: null, library: null, facts: null, activity: null };
const reason = (error: unknown) => (error instanceof Error ? error.message : String(error));

/**
 * `fresh` goes up whenever the page hears of a task, card, note, brief or state change, `made` when
 * something was made or dropped. A task's every step changes `fresh` but not the library, so the
 * library, the brief, and what is remembered are read again only when `made` moved, or every few
 * minutes; the overview and the activity are read each time.
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
    Promise.allSettled([
      api.overview(),
      read(() => api.today(), all),
      read(() => api.library(), all),
      read(() => api.facts(), all),
      api.activity(ACTIVITY_ROWS),
    ]).then((results) => {
      if (!current) return;
      const [overview, today, library, facts, activity] = results;
      // One that failed keeps what it had, and so does one that was not asked for; the others are shown.
      const take = <T,>(result: PromiseSettledResult<T | undefined>, old: T | null) =>
        result.status === "fulfilled" && result.value !== undefined ? result.value : old;
      setData((old) => ({
        overview: take(overview, old.overview),
        today: take(today, old.today),
        library: take(library, old.library),
        facts: take(facts, old.facts),
        activity: take(activity, old.activity),
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

  return { ...data, error, refresh, forget };
}
