// App state: folds the server's messages into one model and decides between the live server and the demo.

import { useCallback, useEffect, useReducer, useState } from "react";
import { type Api, type AppState, type ServerMessage, type Source, type Speaker, openSocket, rest } from "./api";
import { createDemo } from "./demo";

/** One reply on the main card, from the first "thinking" notice to the finished text. */
export interface Suggestion {
  id: number;
  question: string;
  text: string;
  sources: Source[];
  seconds: number | null; // null until the final message arrives
  at: string | null;
  phase: "thinking" | "streaming" | "done" | "skipped";
  since: number; // local clock when it started, for the running timer
}

export type HistoryItem =
  | { key: number; kind: "suggestion"; item: Suggestion }
  | { key: number; kind: "line"; speaker: Speaker | "Info"; text: string; at: string };

/** What the assistant has reported so far about the question with this id. */
export interface AskStream {
  id: string | null;
  steps: string[]; // what it looked up, oldest first
  text: string; // the answer so far
}

export interface Model {
  app: AppState;
  partial: Record<Speaker, string>;
  current: Suggestion | null;
  history: HistoryItem[]; // newest first
  info: { text: string; warn: boolean; at: number } | null;
  seq: number; // next history key
  ask: AskStream;
  fresh: number; // goes up when notes or a brief arrive, so Today reads them again
}

export type Link = "connecting" | "live" | "offline" | "demo";

/** Latest audio levels. Kept out of React state: they arrive ten times a second and only the waveform reads them. */
export const levels = { system: { level: 0, at: 0 }, mic: { level: 0, at: 0 } };

type Action = { msg: ServerMessage; now: number } | { warn: string; now: number } | { reset: true };

const HISTORY_MAX = 200;
const DEMO_AFTER_MS = 2000;
const STEPS_MAX = 40;
const BLANK: Model = {
  app: {
    status: "idle",
    meeting: null,
    engagements: [],
    auto: true,
    app: false,
    call: { active: false, app: null },
    notices: [],
    last_sync: null,
    syncing: false,
  },
  partial: { Them: "", You: "" },
  current: null,
  history: [],
  info: null,
  seq: 0,
  ask: { id: null, steps: [], text: "" },
  fresh: 0,
};

/** The stream for question `id`, started afresh if the messages so far were about another one. */
function stream(m: Model, id: string | undefined): AskStream {
  return m.ask.id === (id ?? null) ? m.ask : { id: id ?? null, steps: [], text: "" };
}

function remember(m: Model, entry: (key: number) => HistoryItem): Model {
  return { ...m, seq: m.seq + 1, history: [entry(m.seq), ...m.history].slice(0, HISTORY_MAX) };
}

/** Clears the main card, keeping whatever text it showed in history. */
function shelve(m: Model): Model {
  const item = m.current;
  if (!item?.text) return { ...m, current: null };
  return remember({ ...m, current: null }, (key) => ({ key, kind: "suggestion", item }));
}

/** Puts suggestion `id` on the main card, shelving a different one if it was there. */
function focus(m: Model, id: number, now: number): [Model, Suggestion] {
  if (m.current?.id === id) return [m, m.current];
  return [
    shelve(m),
    { id, question: "", text: "", sources: [], seconds: null, at: null, phase: "thinking", since: now },
  ];
}

/** A reply still pending when its meeting ends will never finish: keep the text it got, or drop it. */
function settle(item: Suggestion | null): Suggestion | null {
  if (!item || item.phase === "done" || item.phase === "skipped") return item;
  return item.text ? { ...item, phase: "done" } : null;
}

function reduce(m: Model, action: Action): Model {
  if ("reset" in action) return BLANK;
  if ("warn" in action) return { ...m, info: { text: action.warn, warn: true, at: action.now } };
  const { msg, now } = action;
  switch (msg.type) {
    case "state": {
      const { type: _type, ...next } = msg;
      const app: AppState = { ...m.app, ...next };
      const started = app.meeting?.started_at;
      // A meeting we have not seen before starts with a clean transcript.
      if (started && started !== m.app.meeting?.started_at) {
        return { ...BLANK, app, info: m.info, seq: m.seq, ask: m.ask, fresh: m.fresh };
      }
      return app.meeting ? { ...m, app } : { ...m, app, partial: BLANK.partial, current: settle(m.current) };
    }
    case "partial":
      return { ...m, partial: { ...m.partial, [msg.speaker]: msg.text } };
    case "sentence": {
      const { speaker, text, at } = msg;
      const cleared = { ...m, partial: { ...m.partial, [speaker]: "" } };
      return remember(cleared, (key) => ({ key, kind: "line", speaker, text, at }));
    }
    case "thinking": {
      const [base, item] = focus(m, msg.id, now);
      return { ...base, current: { ...item, question: msg.question } };
    }
    case "suggestion_delta": {
      const [base, item] = focus(m, msg.id, now);
      return { ...base, current: { ...item, text: msg.text, phase: "streaming" } };
    }
    case "suggestion": {
      const [base, item] = focus(m, msg.id, now);
      const { question, text, sources, seconds, at } = msg;
      return { ...base, current: { ...item, question, text, sources: sources ?? [], seconds, at, phase: "done" } };
    }
    case "no_suggestion":
      return m.current?.id === msg.id ? { ...m, current: { ...m.current, phase: "skipped" } } : m;
    case "info": {
      // Shown as a toast and kept in the history: a notice such as why a meeting stopped should outlast its toast.
      const at = new Date(now).toISOString();
      const noted = { ...m, info: { text: msg.text, warn: false, at: now } };
      return remember(noted, (key) => ({ key, kind: "line", speaker: "Info", text: msg.text, at }));
    }
    case "ask_step": {
      const ask = stream(m, msg.id);
      return { ...m, ask: { ...ask, steps: [...ask.steps, msg.text].slice(-STEPS_MAX) } };
    }
    case "ask_delta":
      return { ...m, ask: { ...stream(m, msg.id), text: msg.text } };
    case "notes":
      return { ...m, fresh: m.fresh + 1, info: { text: `Notes ready: ${msg.title}`, warn: false, at: now } };
    case "brief":
      return { ...m, fresh: m.fresh + 1, info: { text: "Your brief for today is ready", warn: false, at: now } };
    default:
      return m; // levels are handled before the reducer; unknown types are ignored
  }
}

export function useJarvis() {
  const [model, dispatch] = useReducer(reduce, BLANK);
  const [link, setLink] = useState<Link>("connecting");
  const [api, setApi] = useState<Api>(rest);

  useEffect(() => {
    const receive = (msg: ServerMessage) => {
      if (msg.type === "level") levels[msg.source] = { level: msg.level, at: performance.now() };
      else dispatch({ msg, now: Date.now() });
    };

    let demo: ReturnType<typeof createDemo> | null = null;
    const playDemo = () => {
      dispatch({ reset: true });
      demo = createDemo(receive);
      setApi(demo.api);
      setLink("demo");
    };
    if (new URLSearchParams(location.search).get("demo") === "1") {
      playDemo();
      return () => demo?.stop();
    }

    // Live mode. If the server has not answered within two seconds, play the demo until it does.
    let wasLive = false;
    const fallback = window.setTimeout(playDemo, DEMO_AFTER_MS);
    const close = openSocket(receive, (open) => {
      if (!open) {
        if (wasLive) setLink("offline");
        return;
      }
      clearTimeout(fallback);
      if (demo) {
        demo.stop();
        demo = null;
        dispatch({ reset: true });
      }
      wasLive = true;
      setApi(rest);
      setLink("live");
      rest.state().then(
        (state) => receive({ type: "state", ...state }),
        () => undefined,
      );
    });
    return () => {
      clearTimeout(fallback);
      close();
      demo?.stop();
    };
  }, []);

  /** Runs one server call; a returned state is applied at once and a failure becomes a warning toast. */
  const run = useCallback(
    async (job: (api: Api) => Promise<AppState | { ok: true } | { account: string }>) => {
      try {
        const result = await job(api);
        if ("status" in result) dispatch({ msg: { type: "state", ...result }, now: Date.now() });
      } catch (error) {
        dispatch({ warn: error instanceof Error ? error.message : String(error), now: Date.now() });
      }
    },
    [api],
  );
  const start = useCallback(
    (project: string | null, headphones: boolean | null) => run((a) => a.startMeeting(project, headphones)),
    [run],
  );
  const setAuto = useCallback((enabled: boolean) => run((a) => a.setAuto(enabled)), [run]);
  const reconnect = useCallback(() => run((a) => a.reconnect()), [run]);
  const stop = useCallback(() => run((a) => a.stopMeeting()), [run]);
  const suggest = useCallback(() => run((a) => a.suggest()), [run]);

  return { model, link, api, start, stop, suggest, setAuto, reconnect };
}
