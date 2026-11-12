// Scripted stand-in for the server: plays a meeting on a loop using the real message types
// and answers the REST calls with canned data. All names here are made up.

import type { Api, AppState, Engagement, ServerMessage, Source, Speaker } from "./api";

type Beat =
  | { say: Speaker; text: string }
  | { ask: string; reply: string | null; sources?: Source[] } // reply null: the assistant stays quiet
  | { info: string };

const iso = (offsetMs = 0) => new Date(Date.now() + offsetMs).toISOString();
const daysAgo = (days: number) => iso(-days * 86_400_000).slice(0, 10);
const MINUTE = 60_000;

const ENGAGEMENTS: Engagement[] = [
  { name: "Atlas", people: ["Priya", "Ravi"], folders: 4, files: 128 },
  { name: "Borealis", people: ["Meera", "Dev"], folders: 2, files: 57 },
];

const SCRIPT: Beat[] = [
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

const ASK_REPLY = `**Atlas** is on track for the 14th. Three things to keep in view:

- **Sign-off**: Ravi still has two of the five checks open.
- **Cutover**: the rehearsal is booked for Wednesday.
- **Support**: Borealis cover starts the Monday after.

This is a canned demo reply. The [project notes](https://example.com/atlas) would be linked here.`;

export function createDemo(emit: (msg: ServerMessage) => void): { api: Api; stop: () => void } {
  let app: AppState = { status: "idle", meeting: null, engagements: ENGAGEMENTS };
  let run: AbortController | null = null; // set while a meeting is playing
  let meter = 0;
  let talking: Speaker | null = null;
  let nextId = 1;
  let replies: Promise<void> = Promise.resolve(); // replies run one after another, never interleaved

  const update = (patch: Partial<AppState>): AppState => {
    app = { ...app, ...patch };
    emit({ type: "state", ...app });
    return app;
  };

  /** Waits, or rejects as soon as the meeting is stopped; that rejection is what unwinds the script. */
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

  async function speak(speaker: Speaker, text: string, signal: AbortSignal) {
    const words = text.split(" ");
    talking = speaker;
    for (let n = 1; n <= words.length; n++) {
      emit({ type: "partial", speaker, text: words.slice(0, n).join(" ") });
      await pause(120 + Math.random() * 140, signal);
    }
    talking = null;
    emit({ type: "sentence", speaker, text, at: iso() });
  }

  function reply(question: string, text: string | null, sources: Source[], signal: AbortSignal): Promise<void> {
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
        emit({ type: "suggestion", id, question, text, sources, seconds, at: iso() });
      }
      update({ status: "listening" });
    };
    replies = replies.then(job, job);
    return replies;
  }

  async function play(signal: AbortSignal) {
    await pause(900, signal); // "preparing"
    update({ status: "listening" });
    for (;;) {
      for (const beat of SCRIPT) {
        if ("say" in beat) await speak(beat.say, beat.text, signal);
        else if ("info" in beat) emit({ type: "info", text: beat.info });
        else await reply(beat.ask, beat.reply, beat.sources ?? [], signal);
        await pause(800, signal);
      }
      await pause(3000, signal);
      // Loop: a new start time makes it a new meeting, which clears the transcript.
      update({ meeting: app.meeting && { ...app.meeting, started_at: iso() } });
    }
  }

  function begin(project: string | null): AppState {
    end();
    run = new AbortController();
    meter = window.setInterval(() => {
      const voice = (who: Speaker) => (talking === who ? 0.05 + 0.9 * Math.random() ** 2 : 0.01 * Math.random());
      emit({ type: "level", source: "system", level: voice("Them") });
      emit({ type: "level", source: "mic", level: voice("You") });
    }, 100);
    const title = project ? `${project} weekly sync` : "Unplanned call";
    update({ status: "preparing", meeting: { title, engagement: project, started_at: iso() } });
    play(run.signal).catch(() => undefined); // only ever ends by being stopped
    return app;
  }

  function end(): AppState {
    run?.abort();
    run = null;
    clearInterval(meter);
    talking = null;
    return app.meeting ? update({ status: "idle", meeting: null }) : app;
  }

  const api: Api = {
    state: async () => app,
    startMeeting: async (project) => begin(project),
    stopMeeting: async () => end(),
    suggest: async () => {
      if (run) reply("Requested by you", MANUAL_REPLY, [], run.signal).catch(() => undefined);
      return { ok: true };
    },
    ask: (question, web) =>
      new Promise((resolve) => {
        const web_queries = web ? [question, `${question} latest`] : [];
        setTimeout(() => resolve({ text: ASK_REPLY, web_queries, seconds: 1.8 }), 1800);
      }),
    today: async () => ({
      date: new Date().toLocaleDateString("en-CA"), // local YYYY-MM-DD
      meetings: [
        {
          title: "Atlas weekly sync",
          start: iso(-10 * MINUTE),
          end: iso(20 * MINUTE),
          engagement: "Atlas",
          people: ["Priya", "Ravi"],
        },
        {
          title: "Borealis support review",
          start: iso(90 * MINUTE),
          end: iso(135 * MINUTE),
          engagement: "Borealis",
          people: ["Meera", "Dev"],
        },
        { title: "Planning hour", start: iso(240 * MINUTE), end: iso(300 * MINUTE), engagement: null, people: [] },
      ],
      engagements: ENGAGEMENTS,
    }),
  };

  begin("Atlas");
  return { api, stop: end };
}
