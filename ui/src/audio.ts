// Jarvis's voice: plays the `speech` messages in order and measures how loud the output is.
// The daemon decides when to speak; this only plays what it is sent. Nothing here may throw into
// the view: a sentence that cannot be fetched, decoded or played is passed over.

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
const STALE_MS = 6000; // sound that could not start within this long is dropped, not played late
const GAIN = 3.2; // speech sits low in the 0 to 1 range

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
  since: number;
}

const SILENT: Voice = { speaking: false, saying: "" };

let context: AudioContext | null = null;
let analyser: AnalyserNode | null = null;
let samples: Float32Array<ArrayBuffer> | null = null;
let session: Session | null = null;
let view: Voice = SILENT;
let linger = 0;
let skip = 0;
let acting = 0;
let meter = 0;
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

function audio(): AudioContext | null {
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
  } catch {
    context = null; // no sound on this machine; the text is still shown
  }
  return context;
}

/**
 * A plain browser keeps sound locked until the first click or key press, so the first one opens it.
 * The app allows sound without that. Returns the function that takes the listeners away again.
 */
export function arm(): () => void {
  // The app lets sound start by itself, but a context takes a moment to get going: start it now, so the
  // first sentence is not late. A browser would only complain that nobody has clicked yet.
  if (IN_APP) audio();
  const unlock = () => {
    void audio()
      ?.resume()
      .catch(() => undefined);
  };
  const options = { once: true, capture: true } as const;
  window.addEventListener("pointerdown", unlock, options);
  window.addEventListener("keydown", unlock, options);
  return () => {
    window.removeEventListener("pointerdown", unlock, options);
    window.removeEventListener("keydown", unlock, options);
  };
}

function measure(): void {
  meter = requestAnimationFrame(measure);
  if (!analyser || !samples) return;
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
}

/** Plays the next sentence if it is ready and nothing is playing. */
function pump(): void {
  const now = session;
  if (!now || !context || !analyser || now.source) return;
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
      return;
    }
    if (!clip.buffer) {
      now.next += 1;
      continue;
    }
    if (context.state !== "running") {
      // Still locked, or interrupted (Safari does that when the output device changes). Ask for sound
      // again; the next try comes when the state changes, and by then old words are not worth saying.
      if (performance.now() - now.since > STALE_MS) stop();
      else void context.resume().catch(() => undefined);
      return;
    }
    const source = context.createBufferSource();
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
    clearTimeout(linger);
    show({ speaking: true, saying: clip.text });
    if (!meter) meter = requestAnimationFrame(measure);
    try {
      source.start();
    } catch {
      now.source = null;
      now.next += 1;
      continue;
    }
    return;
  }
}

/** Takes one sentence of audio. A different id from the one playing replaces it. */
export function enqueue(msg: SpeechMessage): void {
  const output = audio();
  if (!output) return;
  if (session?.id !== msg.id) {
    stop();
    session = { id: msg.id, clips: new Map(), next: msg.seq, source: null, since: performance.now() };
  }
  const mine = session;
  if (output.state !== "running") void output.resume().catch(() => undefined);
  fetch(msg.url)
    .then((res) => (res.ok ? res.arrayBuffer() : Promise.reject(new Error(String(res.status)))))
    .then((bytes) => output.decodeAudioData(bytes))
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
