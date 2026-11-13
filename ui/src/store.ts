// App state: folds the server's messages into one model and decides between the live server and the demo.

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import {
  type Api,
  type AppState,
  type Line,
  type ServerMessage,
  type Settings,
  type Source,
  type Speaker,
  openSocket,
  rest,
} from "./api";
import * as voice from "./audio";
import { DEMO, SCENE, type View } from "./bridge";
import { createDemo } from "./demo";
import { levels } from "./levels";

/** One reply on the SAY card during a call, from the first "thinking" notice to the finished text. */
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

/** One exchange with Jarvis: what the user said or typed, what Jarvis did about it, and its reply. */
export interface Turn {
  id: string;
  text: string; // empty when Jarvis spoke up by itself
  spoken: boolean;
  steps: string[]; // what it looked up, oldest first
  reply: string;
  phase: "working" | "streaming" | "done" | "error";
  /** Whether the reply is also said aloud. Known once it is finished. */
  speak: boolean | null;
  seconds: number | null;
  since: number;
  /** Local clock when it finished, so the live line can keep the reply up for a while. */
  doneAt: number | null;
}

/** The user's words while the key is held. */
export interface Hearing {
  phase: "off" | "listening" | "heard";
  text: string;
}

export interface Model {
  app: AppState;
  partial: Record<Speaker, string>;
  current: Suggestion | null;
  history: HistoryItem[]; // this call, newest first
  info: { text: string; warn: boolean; at: number } | null;
  seq: number; // next history key
  hearing: Hearing;
  turn: Turn | null; // the exchange in progress, or the last one
  fresh: number; // goes up when anything the dashboard shows may have changed, so it reads again
  made: number; // goes up when Jarvis made, changed or dropped something (a card, notes, a brief)
}

export type Link = "connecting" | "live" | "offline" | "demo";

type Action =
  | { msg: ServerMessage; now: number }
  | { warn: string; now: number }
  | { settings: Partial<Settings> }
  | { reset: true }
  | { forget: true };

const HISTORY_MAX = 200;
const LINES_MAX = 20;
const STEPS_MAX = 40;
const DEMO_AFTER_MS = 2000;
const STUCK_MS = 5000; // a turn the server says is over, that never got its reply, is closed after this
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
    busy: false,
    tasks: [],
    cards: [],
    conversation: [],
    settings: { auto: true, speak: true, auto_prep: true },
    voice: { hear: false, speak: false },
  },
  partial: { Them: "", You: "" },
  current: null,
  history: [],
  info: null,
  seq: 0,
  hearing: { phase: "off", text: "" },
  turn: null,
  fresh: 0,
  made: 0,
};

function remember(m: Model, entry: (key: number) => HistoryItem): Model {
  return { ...m, seq: m.seq + 1, history: [entry(m.seq), ...m.history].slice(0, HISTORY_MAX) };
}

/** Clears the SAY card, keeping whatever text it showed in the call's history. */
function shelve(m: Model): Model {
  const item = m.current;
  if (!item?.text) return { ...m, current: null };
  return remember({ ...m, current: null }, (key) => ({ key, kind: "suggestion", item }));
}

/** Puts suggestion `id` on the SAY card, shelving a different one if it was there. */
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

/** Adds a line to the conversation unless it is already its last line (the server may have sent it too). */
function said(m: Model, role: Line["role"], text: string, at: string): Model {
  const lines = m.app.conversation;
  const last = lines[lines.length - 1];
  if (!text || (last?.role === role && last.text === text)) return m;
  return { ...m, app: { ...m.app, conversation: [...lines, { role, text, at }].slice(-LINES_MAX) } };
}

/** Replaces the entry with the same id, or puts a new one first. */
function upsert<T extends { id: string }>(list: T[], item: T): T[] {
  return list.some((old) => old.id === item.id) ? list.map((old) => (old.id === item.id ? item : old)) : [item, ...list];
}

/** The turn with this id: the current one, or a new one if the messages so far were about another. */
function turnFor(m: Model, id: string, now: number): Turn {
  if (m.turn?.id === id) return m.turn;
  return {
    id,
    text: "",
    spoken: false,
    steps: [],
    reply: "",
    phase: "working",
    speak: null,
    seconds: null,
    since: now,
    doneAt: null,
  };
}

const finished = (turn: Turn) => turn.phase === "done" || turn.phase === "error";

function reduce(m: Model, action: Action): Model {
  if ("reset" in action) return BLANK;
  if ("forget" in action) return { ...m, turn: null, app: { ...m.app, conversation: [] } };
  if ("warn" in action) return { ...m, info: { text: action.warn, warn: true, at: action.now } };
  if ("settings" in action) {
    const settings = { ...m.app.settings, ...action.settings };
    return { ...m, app: { ...m.app, settings, auto: settings.auto } };
  }
  const { msg, now } = action;
  switch (msg.type) {
    case "state": {
      const { type: _type, ...next } = msg;
      // An older server sends `auto` alone; a newer one sends `settings`, which then decides.
      const settings = next.settings ?? { ...m.app.settings, ...(next.auto === undefined ? {} : { auto: next.auto }) };
      const app: AppState = { ...m.app, ...next, settings, auto: settings.auto };
      // A turn the server no longer works on, that never got its reply (the socket dropped), must not
      // leave the orb working for ever.
      let turn = m.turn;
      if (turn && !finished(turn) && !app.busy && now - turn.since > STUCK_MS) {
        turn = { ...turn, phase: turn.reply ? "done" : "error", reply: turn.reply || "No answer came back.", doneAt: now };
      }
      const base = { ...m, app, turn, fresh: m.fresh + 1 };
      const started = app.meeting?.started_at;
      // A call we have not seen before starts with a clean transcript.
      if (started && started !== m.app.meeting?.started_at) {
        return { ...base, partial: BLANK.partial, current: null, history: [] };
      }
      return app.meeting ? base : { ...base, partial: BLANK.partial, current: settle(m.current) };
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
      // Shown as a toast; during a call it is also kept in the call's history, to outlast the toast.
      const at = new Date(now).toISOString();
      const noted = { ...m, info: { text: msg.text, warn: false, at: now } };
      return m.app.meeting ? remember(noted, (key) => ({ key, kind: "line", speaker: "Info", text: msg.text, at })) : noted;
    }
    case "notes": {
      const info = { text: `Notes ready: ${msg.title}`, warn: false, at: now };
      return { ...m, fresh: m.fresh + 1, made: m.made + 1, info };
    }
    case "brief": {
      const info = { text: "Your brief for today is ready", warn: false, at: now };
      return { ...m, fresh: m.fresh + 1, made: m.made + 1, info };
    }

    case "voice":
      if (msg.phase === "cancelled") return { ...m, hearing: BLANK.hearing };
      return { ...m, hearing: { phase: msg.phase === "heard" ? "heard" : "listening", text: msg.text ?? "" } };
    case "turn": {
      const turn = { ...turnFor(m, msg.id, now), text: msg.text, spoken: msg.spoken };
      return said({ ...m, hearing: BLANK.hearing, turn }, "user", msg.text, msg.at);
    }
    case "step": {
      const turn = turnFor(m, msg.id, now);
      if (finished(turn)) return m;
      return { ...m, turn: { ...turn, steps: [...turn.steps, msg.text].slice(-STEPS_MAX) } };
    }
    case "reply_delta": {
      const turn = turnFor(m, msg.id, now);
      if (finished(turn)) return m;
      return { ...m, turn: { ...turn, reply: msg.text, phase: "streaming" } };
    }
    case "reply": {
      const before = turnFor(m, msg.id, now);
      // The socket and the request both bring the finished reply; the second one changes nothing.
      if (before.phase === "done" && before.reply === msg.text) return m;
      const turn: Turn = { ...before, reply: msg.text, phase: "done", speak: msg.speak, seconds: msg.seconds, doneAt: now };
      return said({ ...m, turn }, "jarvis", msg.text, msg.at);
    }
    case "reply_error":
      return { ...m, turn: { ...turnFor(m, msg.id, now), reply: msg.text, phase: "error", doneAt: now } };
    case "say": {
      // Jarvis speaking up by itself. It does not push aside an exchange that is still in progress.
      const busy = m.turn !== null && !finished(m.turn);
      const turn: Turn = { ...turnFor(m, msg.id, now), text: "", reply: msg.text, phase: "done", doneAt: now };
      return said(busy ? m : { ...m, turn }, "jarvis", msg.text, msg.at);
    }
    case "card":
      return { ...m, fresh: m.fresh + 1, made: m.made + 1, app: { ...m.app, cards: upsert(m.app.cards, msg.card) } };
    case "card_gone": {
      const cards = m.app.cards.filter((card) => card.id !== msg.id);
      return { ...m, fresh: m.fresh + 1, made: m.made + 1, app: { ...m.app, cards } };
    }
    case "task": {
      const was = m.app.tasks.find((task) => task.id === msg.task.id);
      const tasks = upsert(m.app.tasks, msg.task);
      const failed = msg.task.status === "failed" && was?.status !== "failed";
      const info = failed
        ? { text: `${msg.task.title} did not finish${msg.task.step ? `: ${msg.task.step}` : ""}`, warn: true, at: now }
        : m.info;
      return { ...m, fresh: m.fresh + 1, info, app: { ...m.app, tasks } };
    }
    default:
      return m; // levels and speech are handled before the reducer; unknown types are ignored
  }
}

const reason = (error: unknown) => (error instanceof Error ? error.message : String(error));

/**
 * `view` decides who may speak: with the panel and the dashboard both open, only the panel plays
 * Jarvis's voice, or every sentence would be said twice.
 */
export function useJarvis(view: View) {
  const [model, dispatch] = useReducer(reduce, BLANK);
  const [link, setLink] = useState<Link>("connecting");
  const [api, setApi] = useState<Api>(rest);
  const [sending, setSending] = useState(false);
  const sound = view === "orb";
  const settings = useRef(model.app.settings);
  settings.current = model.app.settings;

  useEffect(() => {
    const receive = (msg: ServerMessage) => {
      if (msg.type === "level") {
        levels[msg.source] = { level: msg.level, at: performance.now() };
        return;
      }
      if (msg.type === "speech") {
        if (sound) voice.enqueue(msg);
        return;
      }
      // The user is speaking, or a new exchange began: whatever was being said is cut short.
      if (sound && (msg.type === "turn" || (msg.type === "voice" && msg.phase === "listening"))) voice.stop();
      dispatch({ msg, now: Date.now() });
    };

    const disarm = sound ? voice.arm() : undefined;
    let demo: ReturnType<typeof createDemo> | null = null;
    const playDemo = () => {
      dispatch({ reset: true });
      demo = createDemo(receive, SCENE);
      setApi(demo.api);
      setLink("demo");
    };
    if (DEMO) {
      playDemo();
      return () => {
        demo?.stop();
        disarm?.();
        voice.stop();
      };
    }

    // Live mode. If the server has not answered within two seconds, play the demo until it does.
    let wasLive = false;
    const fallback = window.setTimeout(playDemo, DEMO_AFTER_MS);
    const close = openSocket(receive, (open) => {
      if (!open) {
        // Nobody is listening to the key any more, and whatever was heard so far is lost.
        dispatch({ msg: { type: "voice", phase: "cancelled" }, now: Date.now() });
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
      disarm?.();
      voice.stop();
    };
  }, [sound]);

  /** Runs one server call. A returned state is applied at once and a failure becomes a warning toast. */
  const run = useCallback(
    async (job: (api: Api) => Promise<unknown>): Promise<boolean> => {
      try {
        const result = await job(api);
        // Only a state object is applied; a task also has a `status`, and must not be mistaken for one.
        if (result && typeof result === "object" && "meeting" in result) {
          dispatch({ msg: { type: "state", ...(result as Partial<AppState>) }, now: Date.now() });
        }
        return true;
      } catch (error) {
        dispatch({ warn: reason(error), now: Date.now() });
        return false;
      }
    },
    [api],
  );

  const warn = useCallback((text: string) => dispatch({ warn: text, now: Date.now() }), []);
  const start = useCallback(
    (project: string | null, headphones: boolean | null) => run((a) => a.startMeeting(project, headphones)),
    [run],
  );
  const stop = useCallback(() => run((a) => a.stopMeeting()), [run]);
  const suggest = useCallback(() => run((a) => a.suggest()), [run]);
  const reconnect = useCallback(() => run((a) => a.reconnect()), [run]);
  const syncNow = useCallback(() => run((a) => a.syncNow()), [run]);
  const answer = useCallback((task: string, text: string) => run((a) => a.answerTask(task, text)), [run]);
  const stopTask = useCallback((task: string) => run((a) => a.stopTask(task)), [run]);

  /** A typed message. The turn shows at once; the server's messages for it fill in the rest. */
  const talk = useCallback(
    async (text: string) => {
      const id = `u${Date.now().toString(36)}`;
      const at = () => new Date().toISOString();
      voice.stop();
      setSending(true);
      dispatch({ msg: { type: "turn", id, text, spoken: false, at: at() }, now: Date.now() });
      try {
        const reply = await api.talk(text, id);
        // Usually the socket has delivered this already; applying it again changes nothing.
        const done = { id: reply.id || id, text: reply.text, speak: reply.speak, seconds: reply.seconds, at: at() };
        dispatch({ msg: { type: "reply", ...done }, now: Date.now() });
      } catch (error) {
        dispatch({ msg: { type: "reply_error", id, text: reason(error) }, now: Date.now() });
      } finally {
        setSending(false);
      }
    },
    [api],
  );

  /** Forget the conversation so far: the next message starts from nothing. */
  const forget = useCallback(() => {
    voice.stop();
    dispatch({ forget: true });
    return run((a) => a.resetTalk());
  }, [run]);

  const dismiss = useCallback(
    (card: string) => {
      dispatch({ msg: { type: "card_gone", id: card }, now: Date.now() }); // gone at once; the server follows
      return run((a) => a.dismissCard(card));
    },
    [run],
  );

  /** The switch moves at once; if the server refuses, it moves back. */
  const saveSettings = useCallback(
    async (change: Partial<Settings>) => {
      const before = { ...settings.current };
      dispatch({ settings: change });
      const saved = await run((a) => a.saveSettings(change));
      if (!saved) {
        const undo: Partial<Settings> = {};
        for (const key of Object.keys(change) as (keyof Settings)[]) undo[key] = before[key];
        dispatch({ settings: undo });
      }
      return saved;
    },
    [run],
  );

  return {
    model,
    link,
    api,
    sending,
    warn,
    start,
    stop,
    suggest,
    reconnect,
    syncNow,
    talk,
    forget,
    answer,
    stopTask,
    dismiss,
    saveSettings,
  };
}

export type Jarvis = ReturnType<typeof useJarvis>;
