// The small orb. Inside the app the panel can shrink to a small square that shows only the orb, so
// that Jarvis is a presence on the screen and not a window in the way. The page decides when: it
// asks the app (see `resize` in bridge.ts), and the app answers by putting the class `jarvis-mini`
// on <html>. In a plain browser there is nobody to ask, and nothing shrinks.
//
// The rules: small after 25 seconds with nothing to show; whole again as soon as there is something
// to read or to answer, and when the small orb is clicked. One switch in the dashboard turns the
// shrinking off. It is kept in localStorage, which the panel and the dashboard share. Only the app (or
// the demo, standing in for it) answers a request for a size: in a plain browser nothing shrinks.

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { SIZED, resize } from "./bridge";

const KEY = "jarvis.mini"; // "0": never shrink. Anything else, or nothing at all: shrink when idle.
const CLASS = "jarvis-mini";
const TEXT_BOXES = "input, textarea";
export const MINI_AFTER_MS = 25_000;
const RECHECK_MS = 3000; // how often the small orb looks whether it has been switched off meanwhile
const MOVE_MS = 1000; // a moving pointer restarts the count at most this often
const REASK_MS = 1000; // the app answers a request well within this; until then it is not asked the same again
const READ_MS_PER_CHAR = 45; // an unhurried reader
const READ_MAX_MS = 60_000;

/** Until when a text that was shown at `since` (local clock, ms) is still being read. */
export const readingEnds = (since: number, text: string): number => since + Math.min(READ_MAX_MS, text.length * READ_MS_PER_CHAR);

// ---------- the switch ----------

const watchers = new Set<() => void>();

function allowed(): boolean {
  try {
    return localStorage.getItem(KEY) !== "0";
  } catch {
    return true; // storage is closed to this page: the default
  }
}

function watchSetting(listener: () => void): () => void {
  // The other window changing it arrives as a storage event; this window's own change is announced below.
  const heard = (event: StorageEvent) => (event.key === KEY || event.key === null) && listener();
  watchers.add(listener);
  window.addEventListener("storage", heard);
  return () => {
    watchers.delete(listener);
    window.removeEventListener("storage", heard);
  };
}

function setAllowed(on: boolean): void {
  try {
    if (on) localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, "0");
  } catch {
    // nowhere to keep it; the switch still moves until the window is reloaded
  }
  watchers.forEach((listener) => listener());
}

/** Whether the panel may shrink by itself, and the way to change that. */
export function useSmallOrbSetting(): [on: boolean, set: (on: boolean) => void] {
  return [useSyncExternalStore(watchSetting, allowed), setAllowed];
}

// ---------- the panel's side ----------

const isMini = () => document.documentElement.classList.contains(CLASS);

function watchForm(listener: () => void): () => void {
  const watch = new MutationObserver(listener);
  watch.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => watch.disconnect();
}

/** Text that was typed and not sent: the user is in the middle of something. */
const drafting = () =>
  [...document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>(TEXT_BOXES)].some((box) => box.value.trim() !== "");

interface Watch {
  /** Something on screen needs the whole panel now: it does not shrink, and a small one opens. */
  busy: boolean;
  /** A name for each thing there is to read. A name that was not there before opens the panel. */
  things: string[];
  /** Local clock time until which the newest text is still being read; the count starts after it. */
  readUntil: number;
}

interface SmallOrb {
  /** The form the app says the panel is in. */
  mini: boolean;
  /** Asks for the whole panel. */
  open: () => void;
  /** For the app to say that the pointer came onto the panel or left it. */
  pointer: (inside: boolean) => void;
}

/** Runs the rules. */
export function useSmallOrb({ busy, things, readUntil }: Watch): SmallOrb {
  const mini = useSyncExternalStore(watchForm, isMini);
  const [may] = useSmallOrbSetting();
  const now = useRef({ busy, readUntil, over: false });
  now.current.busy = busy;
  now.current.readUntil = readUntil;
  const timer = useRef(0);
  const asked = useRef(0); // when the small form was last asked for
  const newest = useRef(0); // when something new to read last arrived
  const known = useRef<Set<string> | null>(null);
  const opening = useRef(0); // when the whole panel was last asked for

  /** Asks for the whole panel, once: several reasons to open often arrive in the same moment. */
  const open = useCallback(() => {
    if (!isMini() || performance.now() - opening.current < REASK_MS) return;
    opening.current = performance.now();
    resize("full");
  }, []);

  /** Starts the count again. Whatever happens on the panel calls this. */
  const arm = useCallback(() => {
    clearTimeout(timer.current);
    if (!SIZED) return;
    // 25 seconds, or longer while the newest text is still being read: those 25 seconds are the usual time to
    // read, so a reply that is read within them waits no longer.
    const wait = Math.max(MINI_AFTER_MS, now.current.readUntil - Date.now());
    timer.current = window.setTimeout(() => {
      // Not now: whatever ends the reason (the pointer leaving, the text being sent) starts the count again.
      if (now.current.busy || now.current.over || document.hidden || drafting() || !allowed() || isMini()) return;
      asked.current = Date.now();
      resize("mini");
    }, wait);
  }, []);

  useEffect(() => {
    if (busy) open();
    arm();
  }, [busy, readUntil, may, arm, open]);

  // Something new to read. What is there when the page starts is not news.
  const names = things.join("\n");
  useEffect(() => {
    const before = known.current;
    known.current = new Set(names ? names.split("\n") : []);
    if (!before || ![...known.current].some((name) => !before.has(name))) return;
    newest.current = Date.now();
    open();
    arm();
  }, [names, arm, open]);

  useEffect(() => {
    if (!mini) return arm(); // whole again: the count starts from here
    // It became small just as something arrived, or while it may not be small at all.
    if (now.current.busy || newest.current > asked.current || !allowed()) return open();
    // The switch lives in the dashboard's window, and its change does not always reach this one as an event.
    const check = setInterval(() => !allowed() && open(), RECHECK_MS);
    return () => clearInterval(check);
  }, [mini, may, arm, open]);

  const pointer = useCallback(
    (inside: boolean) => {
      now.current.over = inside;
      arm();
    },
    [arm],
  );

  // The user is here: typing, clicking, scrolling, or resting the pointer on the panel.
  useEffect(() => {
    if (!SIZED) return;
    const root = document.documentElement;
    let moved = 0;
    const active = () => arm();
    const enter = () => {
      now.current.over = true;
    };
    const move = (event: PointerEvent) => {
      now.current.over = true;
      if (event.timeStamp - moved < MOVE_MS) return;
      moved = event.timeStamp;
      arm();
    };
    const leave = () => pointer(false);
    root.addEventListener("pointerenter", enter);
    root.addEventListener("pointermove", move);
    root.addEventListener("pointerleave", leave);
    // A window that is no longer in front is not told where the pointer goes: it may have left unseen.
    window.addEventListener("blur", leave);
    window.addEventListener("pointerdown", active, true);
    window.addEventListener("keydown", active, true);
    window.addEventListener("input", active, true);
    window.addEventListener("wheel", active, { capture: true, passive: true });
    document.addEventListener("visibilitychange", active);
    return () => {
      clearTimeout(timer.current);
      root.removeEventListener("pointerenter", enter);
      root.removeEventListener("pointermove", move);
      root.removeEventListener("pointerleave", leave);
      window.removeEventListener("blur", leave);
      window.removeEventListener("pointerdown", active, true);
      window.removeEventListener("keydown", active, true);
      window.removeEventListener("input", active, true);
      window.removeEventListener("wheel", active, true);
      document.removeEventListener("visibilitychange", active);
    };
  }, [arm, pointer]);

  return { mini, open, pointer };
}
