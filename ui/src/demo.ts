// Scripted stand-in for the server. It plays one story on a loop, using the real message types and
// answering every REST call: a spoken request to prepare for a call, steps, a questions card, an
// answer, a prep card, then a short call with SAY cards, and its notes. All names here are made up.
// `scene` holds still on one moment instead (see the SCENES), for looking at a single state.

import * as voice from "./audio";
import type { Api, AppState, Card, ServerMessage, Source, Speaker, Task } from "./api";
import {
  COUNTS,
  DRAFT,
  ENGAGEMENTS,
  NOTES_WITH_TITLE,
  PREP_ATLAS,
  at,
  daysAgo,
  demoActivity,
  demoFacts,
  demoLibrary,
  demoSources,
  demoToday,
  minutes,
} from "./demoData";

type Beat =
  | { say: Speaker; text: string }
  | { ask: string; reply: string | null; sources?: Source[] } // reply null: the assistant stays quiet
  | { info: string };

const CALL: Beat[] = [
  { say: "Them", text: "So where are we on the Atlas migration, are we still on track for the fourteenth?" },
  {
    ask: "Are we still on track for the fourteenth?",
    reply:
      "Yes, the fourteenth holds, provided the data sign-off lands by Friday.\n" +
      "Also: Ravi owns the sign-off, and two of the five checks are still open.",
    sources: [
      { kind: "notes", title: "Atlas weekly sync", date: daysAgo(7) },
      { kind: "file", title: "Migration plan v3", date: daysAgo(12) },
    ],
  },
  { say: "You", text: "Yes, the fourteenth holds as long as the sign-off lands by Friday." },
  { say: "Them", text: "Good. And what did we agree with Borealis about support hours?" },
  {
    ask: "What did we agree with Borealis about support hours?",
    reply:
      "We agreed weekday cover from nine to six, with a four hour response on priority one issues.\n" +
      "Also: Weekend cover moved to phase two, pending Meera's budget review.",
    sources: [
      { kind: "email", title: "Borealis support terms", date: daysAgo(3) },
      { kind: "notes", title: "Borealis kickoff", date: daysAgo(21) },
    ],
  },
  { info: "Notes for Atlas refreshed" },
  { say: "Them", text: "Perfect. Give me a second, I'll share my screen." },
  { ask: "Give me a second, I'll share my screen.", reply: null },
  { say: "You", text: "Sure, go ahead." },
];

const MANUAL_REPLY = "Offer to send a written summary after the call.\nAlso: Priya asked for one last week.";

const REQUEST = "Prepare me for the Atlas weekly sync.";
const ACK = "On it. I will put the Atlas sheet together, and I have two questions first.";
const QUESTIONS = ["Should the sheet cover the two open sign-off checks?", "Will anyone from Borealis join the rehearsal?"];
const READY = "The Atlas prep is ready. Two checks are still open, and weekend cover is not agreed yet.";
const TYPED_REPLY = "Atlas is on track for the fourteenth. Ravi still has two sign-off checks open.";
const SIGNIN = { key: "signin", text: "Google sign-in has expired. Reconnect to keep syncing." };

/** A card as the story writes it; the time is put on when it is shown. */
type Draft = Omit<Card, "at">;
const PREP_TITLE = "Prep: Atlas weekly sync";
const CARDS: Record<"questions" | "prep" | "notes" | "draft" | "file", Draft> = {
  questions: { id: "c1", kind: "questions", title: PREP_TITLE, body: "", task: "t1", questions: QUESTIONS },
  prep: {
    id: "p1",
    kind: "prep",
    title: PREP_TITLE,
    body: PREP_ATLAS,
    task: "t1",
    path: "~/Documents/Atlas/prep/atlas-weekly-sync.md",
  },
  notes: {
    id: "n1",
    kind: "note",
    title: "Notes: Atlas weekly sync",
    body: NOTES_WITH_TITLE,
    path: "~/Documents/Notes/atlas-weekly-sync.md",
  },
  draft: {
    id: "d1",
    kind: "draft",
    title: "Draft: summary for Priya",
    body: DRAFT,
    link: "https://example.com/mail/drafts/d1",
  },
  file: { id: "f1", kind: "file", title: "cutover-checklist.md", body: "", path: "~/Documents/Atlas/cutover-checklist.md" },
};

/** The moments `?scene=` can hold still on. */
const SCENES = ["idle", "listening", "thinking", "speaking", "questions", "cards", "call", "overview"] as const;

const sentences = (text: string) => text.match(/[^.!?]+[.!?]*\s*/g)?.map((s) => s.trim()) ?? [text];

const INITIAL = (): AppState => ({
  status: "idle",
  meeting: null,
  engagements: ENGAGEMENTS,
  auto: true,
  app: true,
  call: { active: false, app: null },
  notices: [],
  last_sync: at(-minutes(4)),
  syncing: false,
  busy: false,
  tasks: [],
  cards: [],
  conversation: [],
  settings: { auto: true, speak: true, auto_prep: true },
  voice: { hear: true, speak: true },
});

export function createDemo(send: (msg: ServerMessage) => void, scene: string | null): { api: Api; stop: () => void } {
  const world = new AbortController();
  // Once the demo is stopped nothing it still has in flight may reach the page.
  const emit = (msg: ServerMessage) => {
    if (!world.signal.aborted) send(msg);
  };
  let app = INITIAL();
  let facts = demoFacts();
  let activity = demoActivity();
  let call: AbortController | null = null; // set while a call is playing
  let meter = 0;
  let talking: Speaker | null = null;
  let nextId = 1;
  let nextTurn = 1;
  let replies: Promise<void> = Promise.resolve(); // replies run one after another, never interleaved
  let answerer: ((text: string | null) => void) | null = null; // set while the story waits for an answer
  const timers = new Set<number>();

  const update = (patch: Partial<AppState> = {}): AppState => {
    app = { ...app, ...patch };
    emit({ type: "state", ...app });
    return app;
  };
  const log = (kind: string, text: string) => {
    activity = [{ at: at(), kind, text }, ...activity];
  };
  const note = (role: "user" | "jarvis", text: string) => {
    app = { ...app, conversation: [...app.conversation, { role, text, at: at() }].slice(-10) };
  };
  const putCard = (draft: Draft) => {
    const card: Card = { ...draft, at: at() };
    app = { ...app, cards: [card, ...app.cards.filter((old) => old.id !== card.id)] };
    emit({ type: "card", card });
  };
  const dropCard = (id: string) => {
    if (!app.cards.some((card) => card.id === id)) return;
    app = { ...app, cards: app.cards.filter((card) => card.id !== id) };
    emit({ type: "card_gone", id });
  };
  const putTask = (task: Task) => {
    const stamped = { ...task, updated_at: at() };
    const tasks = app.tasks.some((old) => old.id === task.id)
      ? app.tasks.map((old) => (old.id === task.id ? stamped : old))
      : [stamped, ...app.tasks];
    app = { ...app, tasks };
    emit({ type: "task", task: stamped });
  };
  /** The Atlas preparation, in whatever state the story or the scene is at. */
  const prepTask = (change: Partial<Task> = {}): Task => ({
    id: "t1",
    kind: "prep",
    title: PREP_TITLE,
    engagement: "Atlas",
    status: "working",
    step: "reading the Atlas notes",
    questions: [],
    started_at: at(),
    updated_at: at(),
    ...change,
  });

  /** Waits, or rejects as soon as the story is stopped; that rejection is what unwinds the script. */
  const pause = (ms: number, signal: AbortSignal) =>
    new Promise<void>((resolve, reject) => {
      signal.throwIfAborted();
      const stop = () => {
        clearTimeout(timer);
        reject(signal.reason);
      };
      const timer = setTimeout(() => {
        signal.removeEventListener("abort", stop);
        resolve();
      }, ms);
      signal.addEventListener("abort", stop, { once: true });
    });

  const every = (ms: number, job: () => void) => {
    const id = window.setInterval(job, ms);
    timers.add(id);
    return id;
  };
  const clear = (id: number) => {
    clearInterval(id);
    timers.delete(id);
  };

  // --- the user talks to Jarvis ----------------------------------------------------------------

  /** The key is held and the user says `text`: the words appear as they are recognised. */
  async function userSpeaks(text: string, signal: AbortSignal) {
    emit({ type: "voice", phase: "listening" });
    const mic = every(100, () => emit({ type: "level", source: "voice", level: 0.03 + 0.55 * Math.random() ** 2 }));
    try {
      const words = text.split(" ");
      for (let n = 1; n <= words.length; n++) {
        emit({ type: "voice", phase: "partial", text: words.slice(0, n).join(" ") });
        await pause(210 + Math.random() * 140, signal);
      }
      await pause(350, signal);
      emit({ type: "voice", phase: "heard", text });
    } finally {
      clear(mic);
    }
  }

  /** One exchange: the message, what Jarvis looked at, and the reply as it streams in. */
  async function exchange(turn: { text: string; spoken: boolean; steps: string[]; reply: string }, signal: AbortSignal) {
    const id = `d${nextTurn++}`;
    emit({ type: "turn", id, text: turn.text, spoken: turn.spoken, at: at() });
    note("user", turn.text);
    update({ busy: true });
    for (const text of turn.steps) {
      emit({ type: "step", id, text });
      await pause(1000, signal);
    }
    const words = turn.reply.match(/\S+\s*/g) ?? [];
    for (let n = 1; n <= words.length; n += 2) {
      emit({ type: "reply_delta", id, text: words.slice(0, n).join("").trimEnd() });
      await pause(110, signal);
    }
    note("jarvis", turn.reply);
    emit({ type: "reply", id, text: turn.reply, speak: turn.spoken, seconds: 1.8, at: at() });
    update({ busy: false });
    if (turn.spoken) voice.simulate(sentences(turn.reply));
  }

  /** Jarvis speaks up by itself, as it does for a task's questions or result. */
  function speaksUp(text: string) {
    note("jarvis", text);
    emit({ type: "say", id: `s${nextId++}`, text, at: at() });
    voice.simulate(sentences(text));
  }

  // --- a call ----------------------------------------------------------------------------------

  async function speak(speaker: Speaker, text: string, signal: AbortSignal) {
    const words = text.split(" ");
    talking = speaker;
    for (let n = 1; n <= words.length; n++) {
      emit({ type: "partial", speaker, text: words.slice(0, n).join(" ") });
      await pause(120 + Math.random() * 140, signal);
    }
    talking = null;
    emit({ type: "sentence", speaker, text, at: at() });
  }

  function suggestion(question: string, text: string | null, sources: Source[], signal: AbortSignal): Promise<void> {
    const job = async () => {
      signal.throwIfAborted();
      const id = nextId++;
      const began = performance.now();
      update({ status: "thinking" });
      emit({ type: "thinking", id, question });
      await pause(1100, signal);
      if (text === null) {
        emit({ type: "no_suggestion", id });
      } else {
        const words = text.match(/\S+\s*/g) ?? [];
        for (let n = 2; n < words.length; n += 2) {
          emit({ type: "suggestion_delta", id, text: words.slice(0, n).join("") });
          await pause(90, signal);
        }
        const seconds = Math.round((performance.now() - began) / 100) / 10;
        emit({ type: "suggestion", id, question, text, sources, seconds, at: at() });
      }
      update({ status: "listening" });
    };
    replies = replies.then(job, job);
    return replies;
  }

  /** Plays one call from its start to its end, or until it is stopped. */
  async function runCall(project: string | null, outer: AbortSignal) {
    endCall();
    const run = new AbortController();
    call = run;
    const follow = () => run.abort(outer.reason);
    outer.addEventListener("abort", follow, { once: true });
    meter = every(100, () => {
      const level = (who: Speaker) => (talking === who ? 0.05 + 0.9 * Math.random() ** 2 : 0.01 * Math.random());
      emit({ type: "level", source: "system", level: level("Them") });
      emit({ type: "level", source: "mic", level: level("You") });
    });
    update({
      status: "preparing",
      meeting: {
        title: project ? `${project} weekly sync` : "Unplanned call",
        engagement: project,
        started_at: at(),
      },
    });
    try {
      await pause(900, run.signal); // "preparing"
      update({ status: "listening" });
      for (const beat of CALL) {
        if ("say" in beat) await speak(beat.say, beat.text, run.signal);
        else if ("info" in beat) emit({ type: "info", text: beat.info });
        else await suggestion(beat.ask, beat.reply, beat.sources ?? [], run.signal);
        await pause(800, run.signal);
      }
      await pause(2500, run.signal);
    } catch {
      // stopped by the user, or the demo ended
    } finally {
      outer.removeEventListener("abort", follow);
      if (call === run) endCall();
    }
  }

  function endCall(): AppState {
    call?.abort();
    call = null;
    clear(meter);
    talking = null;
    return app.meeting ? update({ status: "idle", meeting: null }) : app;
  }

  // --- the story -------------------------------------------------------------------------------

  async function story(signal: AbortSignal) {
    for (;;) {
      app = INITIAL();
      update();
      await pause(1800, signal);

      // A spoken request, and the work it starts.
      await userSpeaks(REQUEST, signal);
      await exchange(
        { text: REQUEST, spoken: true, steps: ["search: Atlas weekly sync", "read: Migration plan v3"], reply: ACK },
        signal,
      );
      const began = at(); // the preparation keeps its start time through every change
      const prep = (change: Partial<Task> = {}) => prepTask({ started_at: began, ...change });
      putTask(prep());
      log("task", "Started preparing for the Atlas weekly sync");
      await pause(2600, signal);
      putTask(prep({ step: "checking the open sign-off items" }));
      await pause(2600, signal);

      // It needs two answers; the story carries on by itself if nobody gives them.
      putTask(prep({ status: "waiting", step: "waiting for your answer", questions: QUESTIONS }));
      putCard(CARDS.questions);
      speaksUp("I have two questions about the Atlas prep.");
      const answer = await new Promise<string | null>((resolve, reject) => {
        const timer = setTimeout(() => resolve(null), 11_000);
        answerer = (text) => {
          clearTimeout(timer);
          resolve(text);
        };
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
      answerer = null;
      if (answer) await pause(1300, signal); // the card says "Sent" for a moment before it goes
      dropCard("c1");
      if (answer !== "") {
        putTask(prep({ step: "writing the sheet" }));
        await pause(3200, signal);
        putCard(CARDS.prep);
        putTask(prep({ status: "done", step: "the sheet is saved" }));
        log("prep", "Prepared for the Atlas weekly sync");
        speaksUp(READY);
        await pause(8000, signal);
      }

      // The call itself, and what it leaves behind.
      await runCall("Atlas", signal);
      emit({ type: "notes", title: "Atlas weekly sync", engagement: "Atlas", at: at(), text: NOTES_WITH_TITLE });
      putCard(CARDS.notes);
      putCard(CARDS.draft);
      log("notes", "Wrote the notes for the Atlas weekly sync");
      speaksUp("Your notes for the Atlas weekly sync are saved, and I drafted a summary for Priya.");
      await pause(12_000, signal);

      // A notice, then everything is put away and the story starts again.
      update({ notices: [SIGNIN] });
      await pause(7000, signal);
      update({ notices: [] });
      await pause(2500, signal);
    }
  }

  // --- one moment held still, for looking at a single state -----------------------------------

  function hold(name: string) {
    const talked = () => {
      note("user", REQUEST);
      note("jarvis", ACK);
    };
    switch (name) {
      case "listening":
        emit({ type: "voice", phase: "listening" });
        emit({ type: "voice", phase: "partial", text: "Prepare me for the Atlas weekly" });
        every(100, () => emit({ type: "level", source: "voice", level: 0.03 + 0.55 * Math.random() ** 2 }));
        break;
      case "thinking":
        note("user", "What is on my plate today?");
        emit({ type: "turn", id: "h1", text: "What is on my plate today?", spoken: false, at: at() });
        emit({ type: "step", id: "h1", text: "search: Atlas weekly sync" });
        update({ busy: true });
        break;
      case "speaking":
        talked();
        emit({ type: "turn", id: "h1", text: REQUEST, spoken: true, at: at() });
        emit({ type: "reply", id: "h1", text: ACK, speak: true, seconds: 1.8, at: at() });
        voice.simulate(Array.from({ length: 6 }, () => ACK));
        break;
      case "questions":
        talked();
        putTask(prepTask({ status: "waiting", step: "waiting for your answer", questions: QUESTIONS }));
        putCard(CARDS.questions);
        putTask({
          id: "t2",
          kind: "work",
          title: "Find the Borealis support terms",
          engagement: "Borealis",
          status: "working",
          step: "read: Borealis kickoff",
          questions: [],
          started_at: at(-minutes(1)),
          updated_at: at(),
        });
        update();
        break;
      case "cards":
        talked();
        note("user", "What did Meera send about support?");
        note("jarvis", "Meera sent the support terms on Monday. I saved a draft reply for you.");
        putTask(prepTask({ status: "done", step: "the sheet is saved" }));
        for (const draft of [CARDS.file, CARDS.draft, CARDS.notes, CARDS.prep]) putCard(draft);
        update();
        break;
      case "call": {
        const started = at(-minutes(12) - 40_000);
        update({ status: "listening", meeting: { title: "Atlas weekly sync", engagement: "Atlas", started_at: started } });
        emit({ type: "sentence", speaker: "Them", text: "So where are we on the Atlas migration?", at: at(-minutes(1)) });
        emit({ type: "sentence", speaker: "You", text: "We are aiming for the fourteenth.", at: at(-50_000) });
        emit({ type: "thinking", id: 90, question: "Are we still on track for the fourteenth?" });
        const beat = CALL[1];
        if ("ask" in beat && beat.reply) {
          const { ask: question, reply: text, sources = [] } = beat;
          emit({ type: "suggestion", id: 90, question, text, sources, seconds: 2.4, at: at() });
        }
        emit({ type: "partial", speaker: "Them", text: "Good. And what did we agree with Borealis about support hours?" });
        talking = "Them";
        meter = every(100, () => {
          emit({ type: "level", source: "system", level: 0.05 + 0.6 * Math.random() ** 2 });
          emit({ type: "level", source: "mic", level: 0.01 * Math.random() });
        });
        break;
      }
      case "overview":
        talked();
        putTask(prepTask({ step: "writing the sheet", started_at: at(-minutes(2)) }));
        putTask(
          prepTask({
            id: "t0",
            status: "done",
            title: "Prep: Borealis support review",
            engagement: "Borealis",
            step: "the sheet is saved",
            started_at: at(-minutes(180)),
          }),
        );
        update({ notices: [SIGNIN] });
        break;
      default:
        update(); // idle
    }
  }

  // --- the server's side of every REST call ----------------------------------------------------

  const api: Api = {
    state: async () => app,
    startMeeting: async (project) => {
      void runCall(project, world.signal);
      return app;
    },
    stopMeeting: async () => endCall(),
    suggest: async () => {
      if (call) suggestion("Requested by you", MANUAL_REPLY, [], call.signal).catch(() => undefined);
      return { ok: true };
    },
    talk: async (text, id) => {
      // The page showed the message at once. Jarvis looks something up, then answers in text.
      note("user", text);
      update({ busy: true });
      emit({ type: "step", id, text: "search: Atlas status" });
      await new Promise((resolve) => setTimeout(resolve, 900));
      emit({ type: "step", id, text: "read: Migration plan v3" });
      await new Promise((resolve) => setTimeout(resolve, 800));
      note("jarvis", TYPED_REPLY);
      emit({ type: "reply", id, text: TYPED_REPLY, speak: false, seconds: 1.8, at: at() });
      update({ busy: false });
      return { id, text: TYPED_REPLY, speak: false, seconds: 1.8 };
    },
    resetTalk: async () => {
      update({ conversation: [] });
      return { ok: true };
    },
    answerTask: async (id, text) => {
      if (id === "t1") answerer?.(text);
      return { ok: true };
    },
    stopTask: async (id) => {
      const task = app.tasks.find((old) => old.id === id);
      if (task) putTask({ ...task, status: "stopped", step: "stopped" });
      if (id === "t1") {
        dropCard("c1");
        answerer?.("");
      }
      return { ok: true };
    },
    dismissCard: async (id) => {
      dropCard(id);
      return { ok: true };
    },
    saveSettings: async (change) => {
      const settings = { ...app.settings, ...change };
      update({ settings, auto: settings.auto });
      return settings;
    },
    reconnect: async () => {
      update({ notices: [] });
      return { account: "you@example.com" };
    },
    syncNow: async () => {
      update({ syncing: true });
      window.setTimeout(() => {
        log("sync", "Synced mail, calendar and files");
        update({ syncing: false, last_sync: at() });
      }, 1400);
      return { ok: true };
    },
    overview: async () => ({
      status: app.status,
      last_sync: app.last_sync,
      syncing: app.syncing,
      notices: app.notices,
      counts: COUNTS,
      sources: demoSources(),
      engagements: ENGAGEMENTS,
      tasks: app.tasks,
    }),
    today: async () => demoToday(),
    library: async () => demoLibrary(),
    facts: async () => facts,
    forgetFact: async (id) => {
      facts = facts.filter((fact) => fact.id !== id);
      return { ok: true };
    },
    activity: async (limit) => activity.slice(0, limit),
  };

  if (scene && (SCENES as readonly string[]).includes(scene)) hold(scene);
  else story(world.signal).catch(() => undefined); // only ever ends by being stopped

  return {
    api,
    stop: () => {
      world.abort();
      call?.abort();
      timers.forEach((id) => clearInterval(id));
      timers.clear();
    },
  };
}
