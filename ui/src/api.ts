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

export interface AppState {
  status: Status;
  meeting: MeetingInfo | null;
  engagements: Engagement[];
}

export interface Source {
  kind: string;
  title: string;
  date: string;
}

export interface AskReply {
  text: string;
  web_queries: string[];
  seconds: number;
}

export interface TodayMeeting {
  title: string;
  start: string;
  end: string;
  engagement: string | null;
  people: string[];
}

export interface Today {
  date: string;
  meetings: TodayMeeting[];
  engagements: Engagement[];
}

export type ServerMessage =
  | ({ type: "state" } & AppState)
  | { type: "partial"; speaker: Speaker; text: string }
  | { type: "sentence"; speaker: Speaker; text: string; at: string }
  | { type: "thinking"; id: number; question: string }
  | { type: "suggestion_delta"; id: number; text: string }
  | { type: "suggestion"; id: number; question: string; text: string; sources: Source[]; seconds: number; at: string }
  | { type: "no_suggestion"; id: number }
  | { type: "level"; source: "system" | "mic"; level: number }
  | { type: "info"; text: string };

export interface Api {
  state(): Promise<AppState>;
  startMeeting(project: string | null, headphones: boolean): Promise<AppState>;
  stopMeeting(): Promise<AppState>;
  suggest(): Promise<{ ok: true }>;
  ask(question: string, web: boolean): Promise<AskReply>;
  today(): Promise<Today>;
}

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
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
    throw new Error(typeof detail === "string" ? detail : `${method} ${path} failed (${res.status})`);
  }
  return (await res.json()) as T;
}

export const rest: Api = {
  state: () => call("GET", "/api/state"),
  startMeeting: (project, headphones) => call("POST", "/api/meeting/start", { project, headphones }),
  stopMeeting: () => call("POST", "/api/meeting/stop"),
  suggest: () => call("POST", "/api/meeting/suggest"),
  ask: (question, web) => call("POST", "/api/ask", { question, web }),
  today: () => call("GET", "/api/today"),
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
