// Typed client for the Jarvis server: REST under /api and a server-to-client socket at /ws.

export type Status = "idle" | "preparing" | "listening" | "thinking";
export type Speaker = "Them" | "You";

export interface Engagement {
  name: string;
  people: string[];
  folders: number;
  files: number;
}

export interface MeetingInfo {
  title: string;
  engagement: string | null;
  started_at: string;
}

export interface Notice {
  key: string; // "signin" is the one the interface knows how to act on
  text: string;
}

export interface Settings {
  /** Listening starts by itself when a call does. */
  auto: boolean;
  /** A spoken message gets a spoken reply. */
  speak: boolean;
  /** Jarvis prepares for client meetings ahead of time without being asked. */
  auto_prep: boolean;
  /** "Hey Jarvis" wakes it without the key. Absent on a service that has no ear. */
  wake_word?: boolean;
  /** Jarvis may speak up by itself about new mail. Absent on a service that never does. */
  proactive?: boolean;
}

export type CardKind = "note" | "prep" | "questions" | "draft" | "file" | "approval" | "proposal";

/** One thing the user can answer a proposal with: pressing it sends `say` as a typed message. */
export interface CardAction {
  label: string;
  say: string;
}

/**
 * Something Jarvis puts in front of the user: a result, a draft, questions it needs answered,
 * something it will only do after a yes (approval), or something it noticed (proposal).
 */
export interface Card {
  id: string;
  kind: CardKind;
  title: string;
  body: string; // markdown
  at: string;
  task?: string | null;
  questions?: string[];
  link?: string | null;
  path?: string | null;
  /** approval: the id of the question it puts, as `state.approvals` lists it. */
  approval?: string | null;
  /** proposal: up to three ways to answer it. */
  actions?: CardAction[];
  /** proposal: the number of the mail it is about, if any. The actions already say what to do with it. */
  mail?: number | null;
}

/** A yes Jarvis is waiting for. `kind` says what it would do: send_mail, run, write, fetch, read. */
export interface Approval {
  id: string;
  kind: string;
  title: string;
  detail: string; // markdown; the same text as its card's body
}

/** The kinds a yes can cover for the rest of a task, not only this once. */
export const TASK_WIDE = ["run", "write", "fetch"];

export type TaskStatus = "working" | "waiting" | "done" | "failed" | "stopped";

/** Work Jarvis does in the background, such as preparing for a meeting. */
export interface Task {
  id: string;
  kind: "prep" | "work";
  title: string;
  engagement: string | null;
  status: TaskStatus;
  step: string; // the latest thing it did
  questions: string[]; // what it is waiting to hear, when status is "waiting"
  started_at: string;
  updated_at: string;
  /** work: the folder it works in. */
  folder?: string | null;
}

/** Still going: working, or waiting for an answer. */
export const isRunning = (task: Task) => task.status === "working" || task.status === "waiting";

export interface Line {
  role: "user" | "jarvis";
  text: string;
  at: string;
}

export interface AppState {
  status: Status;
  meeting: MeetingInfo | null;
  engagements: Engagement[];
  /** Mirrors settings.auto; older servers send only this. */
  auto: boolean;
  /** The Jarvis app is running and reporting calls. */
  app: boolean;
  call: { active: boolean; app: string | null };
  notices: Notice[];
  last_sync: string | null;
  syncing: boolean;
  /** Jarvis is working on a turn. */
  busy: boolean;
  tasks: Task[];
  cards: Card[]; // newest first
  conversation: Line[]; // oldest first
  settings: Settings;
  /** What this Mac can do: hear the user, and speak. */
  voice: { hear: boolean; speak: boolean };
  /** What waits for the user's yes, oldest first. A question is open exactly as long as it is listed here. */
  approvals: Approval[];
  /** Listening for "Hey Jarvis": `on` while the microphone is open for it, `awake` once it heard its name. */
  ear: { on: boolean; awake: boolean };
}

export interface Source {
  kind: string;
  title: string;
  date: string;
}

export interface TalkReply {
  id: string;
  text: string;
  speak: boolean;
  seconds: number;
}

export interface Note {
  title: string;
  engagement: string | null;
  at: string;
  text: string;
}

export interface Brief {
  date: string;
  at: string;
  text: string;
}

export interface TodayMeeting {
  title: string;
  start: string;
  end: string;
  engagement: string | null;
  people: string[];
  /** The user said it is off: cancelled, skipped or moved. */
  skipped?: boolean;
}

export interface Today {
  date: string;
  meetings: TodayMeeting[];
  engagements: Engagement[];
  brief?: Brief | null;
  notes?: Note[];
}

// --- dashboard data ---

export interface Counts {
  mail: number;
  events: number;
  files: number;
  notes: number;
  preps: number;
  drafts: number;
  created: number;
}

/** One place Jarvis reads from, and how its last sync went. */
export interface SourceStatus {
  name: string;
  account: string;
  last_run: string | null;
  status: string;
  detail: string;
}

export interface Overview {
  status: Status;
  last_sync: string | null;
  syncing: boolean;
  notices: Notice[];
  counts: Counts;
  sources: SourceStatus[];
  engagements: Engagement[];
  tasks: Task[];
}

/** Something Jarvis made and kept: a prep sheet, meeting notes, a brief, a draft, a file. */
export interface Item {
  id: string | number;
  kind: string;
  title: string;
  engagement: string | null;
  at: string | null; // the store can hold a record without a time
  body?: string | null; // markdown
  path?: string | null;
  link?: string | null;
}

export const LIBRARY_KINDS = ["preps", "notes", "briefs", "drafts", "files"] as const;
export type LibraryKind = (typeof LIBRARY_KINDS)[number];
export type Library = Record<LibraryKind, Item[]>;

/** Something the user told Jarvis to remember. */
export interface Fact {
  id: string | number;
  text: string;
  engagement: string | null;
  at: string | null;
}

export interface Activity {
  at: string | null;
  kind: string;
  text: string;
}

/** A time the server sends as text (ISO) or as a number (seconds since 1970). */
export type When = string | number | null;

/** Something Jarvis will say at a given time. */
export interface Reminder {
  id: string | number;
  due: When;
  text: string;
}

/** Who owes what, and by when. `status` is "open" until it is done. */
export interface PromiseEntry {
  id: string | number;
  engagement: string | null;
  owner: string;
  text: string;
  due: When;
  status: string;
}

/** What Jarvis knows about one engagement. Only the parts the dashboard shows are named here. */
export interface Dossier {
  name: string;
  dossier?: string | null; // markdown
  promises?: PromiseEntry[] | null;
}

export type ServerMessage =
  | ({ type: "state" } & Partial<AppState>)
  // a call being listened to
  | { type: "partial"; speaker: Speaker; text: string }
  | { type: "sentence"; speaker: Speaker; text: string; at: string }
  | { type: "thinking"; id: number; question: string }
  | { type: "suggestion_delta"; id: number; text: string }
  | { type: "suggestion"; id: number; question: string; text: string; sources: Source[]; seconds: number; at: string }
  | { type: "no_suggestion"; id: number }
  | { type: "level"; source: "system" | "mic" | "voice"; level: number }
  | { type: "info"; text: string }
  | ({ type: "notes" } & Note)
  | ({ type: "brief" } & Brief)
  // talking to Jarvis
  | { type: "voice"; phase: "listening" | "partial" | "heard" | "cancelled"; text?: string }
  | { type: "ear"; phase: "asleep" | "awake" | "off" }
  | { type: "turn"; id: string; text: string; spoken: boolean; at: string }
  | { type: "step"; id: string; text: string }
  | { type: "reply_delta"; id: string; text: string }
  | { type: "reply"; id: string; text: string; speak: boolean; seconds: number; at: string }
  | { type: "reply_error"; id: string; text: string }
  | { type: "say"; id: string; text: string; at: string }
  | { type: "speech"; id: string; seq: number; text: string; url: string }
  | { type: "card"; card: Card }
  | { type: "card_gone"; id: string }
  | { type: "task"; task: Task };

export type SpeechMessage = Extract<ServerMessage, { type: "speech" }>;

export interface Api {
  state(): Promise<Partial<AppState>>;
  /** `headphones` null leaves it to the app, which knows what the sound is playing through. */
  startMeeting(project: string | null, headphones: boolean | null): Promise<Partial<AppState>>;
  stopMeeting(): Promise<Partial<AppState>>;
  suggest(): Promise<unknown>;
  /** A typed message. `id` comes back on the events for this turn. */
  talk(text: string, id: string): Promise<TalkReply>;
  resetTalk(): Promise<unknown>;
  answerTask(id: string, text: string): Promise<unknown>;
  stopTask(id: string): Promise<unknown>;
  dismissCard(id: string): Promise<unknown>;
  /** The user's answer to an approval. `scope` "task": the yes holds for the rest of that task. */
  settleApproval(id: string, approve: boolean, scope?: "task"): Promise<unknown>;
  /** Tells the service that speech `id` started or stopped sounding. Nothing comes back, and a failure is nobody's business. */
  voicePlaying(id: string, playing: boolean): void;
  saveSettings(change: Partial<Settings>): Promise<unknown>;
  reconnect(): Promise<unknown>;
  syncNow(): Promise<unknown>;
  overview(): Promise<Overview>;
  today(): Promise<Today>;
  library(): Promise<Library>;
  facts(): Promise<Fact[]>;
  forgetFact(id: string | number): Promise<unknown>;
  activity(limit: number): Promise<Activity[]>;
  reminders(): Promise<Reminder[]>;
  promises(): Promise<PromiseEntry[]>;
  engagement(name: string): Promise<Dossier>;
}

/** A request the server answered with an error. `status` tells a missing endpoint from a broken one. */
export class HttpError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

/**
 * For the parts of the service that may not be there yet: an endpoint the server does not have
 * (404, or 405 when only the page's own files answer under that path) reads as null, "nothing to show".
 */
export function optional<T>(job: Promise<T>): Promise<T | null> {
  return job.catch((error: unknown) => {
    if (error instanceof HttpError && (error.status === 404 || error.status === 405)) return null;
    throw error;
  });
}

async function call<T>(method: "GET" | "POST" | "DELETE", path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).catch(() => {
    throw new Error("Jarvis server unreachable");
  });
  if (!res.ok) {
    // Prefer the server's own explanation when it sends one.
    const detail: unknown = await res.json().then(
      (j) => j?.detail,
      () => undefined,
    );
    throw new HttpError(typeof detail === "string" ? detail : `${method} ${path} failed (${res.status})`, res.status);
  }
  return (await res.json()) as T;
}

const seg = encodeURIComponent;

export const rest: Api = {
  state: () => call("GET", "/api/state"),
  startMeeting: (project, headphones) => call("POST", "/api/meeting/start", { project, headphones }),
  stopMeeting: () => call("POST", "/api/meeting/stop"),
  suggest: () => call("POST", "/api/meeting/suggest"),
  talk: (text, id) => call("POST", "/api/talk", { text, spoken: false, id }),
  resetTalk: () => call("POST", "/api/talk/reset"),
  answerTask: (id, text) => call("POST", `/api/tasks/${seg(id)}/answer`, { text }),
  stopTask: (id) => call("POST", `/api/tasks/${seg(id)}/stop`),
  dismissCard: (id) => call("POST", `/api/cards/${seg(id)}/dismiss`),
  settleApproval: (id, approve, scope) => call("POST", `/api/approvals/${seg(id)}`, scope ? { approve, scope } : { approve }),
  voicePlaying: (id, playing) => {
    void call("POST", "/api/voice/playing", { id, playing }).catch(() => undefined);
  },
  saveSettings: (change) => call("POST", "/api/settings", change),
  reconnect: () => call("POST", "/api/reconnect"),
  syncNow: () => call("POST", "/api/sync"),
  overview: () => call("GET", "/api/overview"),
  today: () => call("GET", "/api/today"),
  library: () => call("GET", "/api/library"),
  facts: () => call("GET", "/api/facts"),
  forgetFact: (id) => call("DELETE", `/api/facts/${seg(String(id))}`),
  activity: (limit) => call("GET", `/api/activity?limit=${limit}`),
  reminders: () => call("GET", "/api/reminders"),
  promises: () => call("GET", "/api/promises"),
  engagement: (name) => call("GET", `/api/engagements/${seg(name)}`),
};

const RETRY_MIN_MS = 500;
const RETRY_MAX_MS = 5000;

/**
 * Opens the push socket and keeps it open, retrying with a short doubling backoff.
 * `onLink` reports every open and every drop. Returns a function that closes it for good.
 */
export function openSocket(onMessage: (msg: ServerMessage) => void, onLink: (open: boolean) => void): () => void {
  let socket: WebSocket | null = null;
  let retry = 0;
  let delay = RETRY_MIN_MS;
  let closed = false;

  const connect = () => {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${location.host}/ws`);
    socket.onopen = () => {
      delay = RETRY_MIN_MS;
      onLink(true);
    };
    socket.onmessage = (event) => {
      let msg: ServerMessage | null = null;
      try {
        msg = JSON.parse(event.data as string);
      } catch {
        // not JSON: nothing we can show
      }
      if (msg && typeof msg === "object") onMessage(msg);
    };
    socket.onclose = () => {
      if (closed) return;
      onLink(false);
      retry = window.setTimeout(connect, delay);
      delay = Math.min(delay * 2, RETRY_MAX_MS);
    };
  };

  connect();
  return () => {
    closed = true;
    clearTimeout(retry);
    socket?.close();
  };
}
