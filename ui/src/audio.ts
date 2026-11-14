// Jarvis's voice: plays the `speech` messages in order and measures how loud the output is.
// The daemon decides when to speak; this only plays what it is sent, and tells it when the sound
// starts and stops. Also the one sound the page makes by itself: the short tone when Jarvis wakes
// to its name. Nothing here may throw into the view: a sentence that cannot be fetched, decoded or
// played is passed over.
//
// The Mac's sound output is open only while there is something to play. An audio context that
// runs keeps the output device open even in silence, and other apps can see that: so the one
// context here is made for the first sound, put to sleep two seconds after the last, and woken for
// the next. Nothing else in the page may make a context of its own.

import { useSyncExternalStore } from "react";
import type { SpeechMessage } from "./api";
import { IN_APP } from "./bridge";
import { voiceOut } from "./levels";

/** What the page knows about Jarvis's voice right now. */
export interface Voice {
  /** A sentence is playing. The short gap between two sentences still counts. */
  speaking: boolean;
  /** The words of the sentence being played, so the live line can follow the voice. */
  saying: string;
}

const LINGER_MS = 700; // between sentences the voice still counts as speaking
const SKIP_AFTER_MS = 4000; // a sentence that has not arrived by then is passed over
const STALE_MS = 6000; // sound that waited this long for the output to open is dropped, not played late
const IDLE_MS = 2000; // the output is given back this long after the last sound
const GAIN = 3.2; // speech sits low in the 0 to 1 range
const TONE_GAP_MS = 1000; // two wakes closer together than this make one tone
const TONE_LATE_MS = 800; // a tone that waited longer than this for the output no longer says "I heard you"
const DECODE_HZ = 48_000;

/** One sentence. A null buffer means it could not be fetched or decoded. */
interface Clip {
  buffer: AudioBuffer | null;
  text: string;
}

interface Session {
  id: string;
  clips: Map<number, Clip>;
  next: number;
  source: AudioBufferSourceNode | null;
  /** Since when its next sentence has been waiting for the output to open; 0 while it is not waiting. */
  blocked: number;
}

const SILENT: Voice = { speaking: false, saying: "" };

let context: AudioContext | null = null;
let analyser: AnalyserNode | null = null;
let samples: Float32Array<ArrayBuffer> | null = null;
let decoder: OfflineAudioContext | null = null;
let asleep = true; // the output is closed, or was told to close: the context's own state follows a moment later
let waking = false; // it was told to open and has not answered yet
let idle = 0;
const waiters = new Set<() => void>(); // what runs once the output is open
let session: Session | null = null;
let view: Voice = SILENT;
let linger = 0;
let skip = 0;
let acting = 0;
let meter = 0;
let sounding: string | null = null; // the speech the service was last told is playing
let report: ((id: string, playing: boolean) => void) | null = null;
let toned = 0;
const listeners = new Set<() => void>();

function show(next: Voice): void {
  if (view.speaking === next.speaking && view.saying === next.saying) return;
  view = next;
  listeners.forEach((listener) => listener());
}

const read = () => view;

function watch(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** Whether Jarvis is speaking and what it is saying. Re-renders on each sentence, not on each frame. */
export function useVoice(): Voice {
  return useSyncExternalStore(watch, read);
}

// ---------- the sound output: open only while something plays ----------

/** The one context, made when the first sound is about to play. Null on a machine without sound. */
function output(): AudioContext | null {
  // Safari can close the context itself, when the sound device goes away: then a fresh one is made.
  if (context?.state === "closed") {
    context = null;
    analyser = null;
  }
  if (context) return context;
  try {
    context = new AudioContext();
    analyser = context.createAnalyser();
    analyser.fftSize = 512;
    analyser.connect(context.destination);
    samples = new Float32Array(analyser.fftSize);
    context.onstatechange = () => pump();
    asleep = true; // until it says that it runs
  } catch {
    context = null; // no sound on this machine; the text is still shown
  }
  return context;
}

/**
 * Runs `play` with the output open: at once if it is, otherwise after waking it. A context that is
 * asleep keeps its place in time, so what is started then begins cleanly, without a click. In a
 * plain browser the output stays locked until the first click, and `play` waits for that.
 */
function whenOpen(play: () => void): void {
  const out = output();
  if (!out) return;
  clearTimeout(idle);
  if (!asleep && !waking && out.state === "running") {
    play();
    return;
  }
  waiters.add(play);
  if (waking) return;
  waking = true;
  out.resume().then(
    () => {
      waking = false;
      asleep = false;
      rest(); // whatever plays now holds the output open; if nothing does, it closes again
      const ready = [...waiters];
      waiters.clear();
      ready.forEach((run) => run());
    },
    () => {
      waking = false;
      waiters.clear();
    },
  );
}

/** Nothing is playing any more: the output closes two seconds after `after` ms, unless sound comes first. */
function rest(after = 0): void {
  clearTimeout(idle);
  idle = window.setTimeout(() => {
    if (!context || session?.source || waking) return;
    asleep = true;
    void context.suspend().catch(() => undefined);
  }, after + IDLE_MS);
}

/**
 * Decoding needs a context, but not a sound device: an offline one does it, so the real one can stay
 * closed until there is something to play.
 */
function decode(bytes: ArrayBuffer): Promise<AudioBuffer> {
  decoder ??= new OfflineAudioContext(1, 1, DECODE_HZ);
  return decoder.decodeAudioData(bytes);
}

/**
 * A plain browser keeps sound locked until the first click or key press, so the first one opens the
 * output once; after that the page may open it by itself, and it closes again as after any sound.
 * The app allows sound without that. Returns the function that takes the listeners away again.
 */
export function arm(): () => void {
  if (IN_APP) return () => undefined;
  const unlock = () => {
    // Asked for here, inside the click itself: a wake that was asked for before it does not count as the user's.
    void output()
      ?.resume()
      .catch(() => undefined);
    whenOpen(() => undefined);
  };
  const options = { once: true, capture: true } as const;
  window.addEventListener("pointerdown", unlock, options);
  window.addEventListener("keydown", unlock, options);
  return () => {
    window.removeEventListener("pointerdown", unlock, options);
    window.removeEventListener("keydown", unlock, options);
  };
}

// ---------- telling the service ----------

/**
 * Who is told when a speech starts and stops sounding: the service, which must not take Jarvis's
 * own voice for the user's, and listens for a follow-up once it is quiet again.
 */
export function onPlaying(listener: ((id: string, playing: boolean) => void) | null): void {
  report = listener;
}

/** Notes which speech is sounding, `null` for none, and reports the change. */
function tell(id: string | null): void {
  if (sounding === id) return;
  if (sounding !== null) report?.(sounding, false);
  sounding = id;
  if (id !== null) report?.(id, true);
}

// ---------- speech ----------

function measure(): void {
  meter = requestAnimationFrame(measure);
  if (!analyser || !samples) return;
  // A context that is not running has nothing new to measure, and what its analyser last saw is not the
  // voice now: the meter settles to zero instead of holding that.
  if (context?.state !== "running") {
    voiceOut.level *= 0.5;
    return;
  }
  analyser.getFloatTimeDomainData(samples);
  let sum = 0;
  for (const sample of samples) sum += sample * sample;
  const level = Math.min(1, Math.sqrt(sum / samples.length) * GAIN);
  voiceOut.level += (level - voiceOut.level) * 0.5;
}

function quiet(): void {
  cancelAnimationFrame(meter);
  meter = 0;
  voiceOut.level = 0;
  show(SILENT);
  tell(null);
}

/** Plays the next sentence if it is ready and nothing is playing. */
function pump(): void {
  const now = session;
  if (!now || now.source) return;
  clearTimeout(skip);
  for (;;) {
    const clip = now.clips.get(now.next);
    if (!clip) {
      // Not here yet. If later sentences are waiting, give this one a while, then pass over it.
      const later = [...now.clips.keys()].filter((seq) => seq > now.next);
      if (later.length) {
        skip = window.setTimeout(() => {
          if (session !== now || now.source || now.clips.has(now.next)) return;
          now.next = Math.min(...later);
          pump();
        }, SKIP_AFTER_MS);
      }
      clearTimeout(linger);
      linger = window.setTimeout(() => !session?.source && quiet(), LINGER_MS);
      if (!asleep) rest(); // the queue is empty: the output is given back unless the next sentence comes soon
      return;
    }
    if (!clip.buffer) {
      now.next += 1;
      continue;
    }
    const out = output();
    if (!out || !analyser) return;
    if (asleep || waking || out.state !== "running") {
      // Closed because nothing was playing, still locked, or interrupted (Safari does that when the
      // output device changes). Open it; the next try comes when it runs, and by then old words
      // are not worth saying.
      now.blocked ||= performance.now();
      if (performance.now() - now.blocked > STALE_MS) stop();
      else whenOpen(pump);
      return;
    }
    const source = out.createBufferSource();
    source.buffer = clip.buffer;
    source.connect(analyser);
    source.onended = () => {
      if (now.source !== source) return;
      now.source = null;
      now.clips.delete(now.next);
      now.next += 1;
      pump();
    };
    now.source = source;
    now.blocked = 0;
    clearTimeout(linger);
    clearTimeout(idle);
    show({ speaking: true, saying: clip.text });
    if (!meter) meter = requestAnimationFrame(measure);
    try {
      source.start();
    } catch {
      now.source = null;
      now.next += 1;
      continue;
    }
    tell(now.id); // the gap before the next sentence still counts as playing: see LINGER_MS
    return;
  }
}

/** Takes one sentence of audio. A different id from the one playing replaces it. */
export function enqueue(msg: SpeechMessage): void {
  if (typeof AudioContext === "undefined") return; // no sound on this machine; the text is still shown
  if (session?.id !== msg.id) {
    stop();
    session = { id: msg.id, clips: new Map(), next: msg.seq, source: null, blocked: 0 };
  }
  const mine = session;
  fetch(msg.url)
    .then((res) => (res.ok ? res.arrayBuffer() : Promise.reject(new Error(String(res.status)))))
    .then(decode)
    .then(
      (buffer) => buffer,
      () => null,
    )
    .then((buffer) => {
      if (session !== mine) return; // replaced meanwhile
      mine.clips.set(msg.seq, { buffer, text: msg.text });
      pump();
    });
}

/** Stops talking and forgets what was queued: the user is speaking, or a new turn began. */
export function stop(): void {
  clearTimeout(skip);
  clearTimeout(linger);
  clearTimeout(acting);
  const source = session?.source;
  session = null;
  if (source) {
    source.onended = null;
    try {
      source.stop();
    } catch {
      // it had not started
    }
  }
  quiet();
  if (context && !asleep) rest();
}

/**
 * For the demo: acts out saying these sentences with no sound, so the orb and the live line can be
 * watched working. Anything real, or `stop()`, ends it.
 */
export function simulate(sentences: string[], sentenceMs = 2600): void {
  stop();
  const began = performance.now();
  let n = 0;
  const next = () => {
    if (n >= sentences.length) return quiet();
    show({ speaking: true, saying: sentences[n++] });
    acting = window.setTimeout(next, sentenceMs);
  };
  const wobble = () => {
    meter = requestAnimationFrame(wobble);
    const t = (performance.now() - began) / 1000;
    const target = 0.22 + 0.5 * Math.abs(Math.sin(t * 7.3)) * (0.6 + 0.4 * Math.sin(t * 2.1));
    voiceOut.level += (target - voiceOut.level) * 0.35;
  };
  meter = requestAnimationFrame(wobble);
  next();
}

// ---------- the wake tone ----------

/** Two soft notes, the second a fifth above the first. [hertz, seconds after the start] */
const TONE: [number, number][] = [
  [587.33, 0],
  [880, 0.085],
];
const TONE_NOTE_S = 0.16;
const TONE_PEAK = 0.07; // quiet: it says "I heard you", it does not ask for attention

/**
 * Schedules the wake tone on `out`, starting at its clock time `at`, and returns how many seconds
 * it lasts. Each note fades in over a few milliseconds and dies away, so nothing clicks. It takes any
 * audio context, so that it can be rendered to a buffer and looked at without making a sound.
 */
export function wakeTone(out: BaseAudioContext, at: number): number {
  for (const [hertz, after] of TONE) {
    const start = at + after;
    const note = out.createOscillator();
    const fade = out.createGain();
    note.type = "sine";
    note.frequency.value = hertz;
    fade.gain.setValueAtTime(0, start);
    fade.gain.linearRampToValueAtTime(TONE_PEAK, start + 0.012);
    fade.gain.exponentialRampToValueAtTime(0.001, start + TONE_NOTE_S);
    note.connect(fade);
    fade.connect(out.destination); // not through the analyser: the orb must not take it for speech
    note.start(start);
    note.stop(start + TONE_NOTE_S + 0.01);
  }
  return TONE[TONE.length - 1][1] + TONE_NOTE_S + 0.01;
}

/** Plays the wake tone: the output is opened for it, and closes again two seconds after it. */
export function chime(): void {
  const asked = performance.now();
  if (asked - toned < TONE_GAP_MS) return;
  toned = asked;
  whenOpen(() => {
    // In a browser that keeps sound locked until a click, this runs whenever that click comes: long after the wake.
    if (!context || performance.now() - asked > TONE_LATE_MS) return;
    try {
      rest(wakeTone(context, context.currentTime + 0.01) * 1000);
    } catch {
      // no tone; the orb still shows that it woke
    }
  });
}
