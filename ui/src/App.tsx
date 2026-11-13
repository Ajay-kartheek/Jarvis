import { useEffect, useRef, useState } from "react";
import { AnimatePresence, MotionConfig, motion, useReducedMotion } from "motion/react";
import { Ask, useAsk } from "./screens/Ask";
import { Meeting } from "./screens/Meeting";
import { Today } from "./screens/Today";
import type { Notice } from "./api";
import { type Link, type Model, useJarvis } from "./store";

declare global {
  interface Window {
    /** Set here, called by the Jarvis app each time it brings its panel forward. */
    jarvisNative?: { onShow?: (reason: string) => void };
    webkit?: { messageHandlers?: { jarvis?: { postMessage: (message: unknown) => void } } };
  }
}

const MODES = ["meeting", "ask", "today"] as const;
type Mode = (typeof MODES)[number];

const LINK_LABEL: Record<Link, string> = { connecting: "Linking", live: "Live", offline: "Offline", demo: "Demo" };
const TYPING = 'input:not([type="checkbox"]), textarea, select'; // where a digit is text, not a shortcut
const BOOT_MS = 1600; // frames draw in slowly during this window, briskly afterwards
const QUERY = new URLSearchParams(location.search);
const PANEL = QUERY.get("panel") === "1"; // inside the Jarvis app's floating panel
const FIRST: Mode = MODES.find((name) => name === QUERY.get("tab")) ?? "meeting"; // ?tab=today opens there

/** Asks the Jarvis app to put its panel away. Control+Option+J brings it back. */
const hidePanel = () => window.webkit?.messageHandlers?.jarvis?.postMessage({ action: "hide" });
const TOAST_MS = 6000;

export default function App() {
  const jarvis = useJarvis();
  const ask = useAsk(jarvis.api, jarvis.model.ask);
  const reduced = useReducedMotion();
  const [mode, setMode] = useState<Mode>(FIRST);
  const [booting, setBooting] = useState(true);
  const { model, suggest } = jarvis;
  const inMeeting = model.app.meeting !== null;
  const busy = model.app.status !== "idle";

  // Nothing to press: the screen follows what is happening. A call brings up the meeting,
  // new notes or a brief bring up Today.
  const wasBusy = useRef(busy);
  useEffect(() => {
    if (busy && !wasBusy.current) setMode("meeting");
    wasBusy.current = busy;
  }, [busy]);
  const seenFresh = useRef(model.fresh);
  useEffect(() => {
    if (model.fresh > seenFresh.current && !busy) setMode("today");
    seenFresh.current = model.fresh;
  }, [model.fresh, busy]);

  // The app's shortcut opens the panel ready for a question, unless a call is being followed.
  useEffect(() => {
    window.jarvisNative = {
      onShow: (reason) => {
        if (reason !== "hotkey" || busy) return;
        setMode("ask");
        requestAnimationFrame(() => document.querySelector<HTMLElement>(".ask-input")?.focus());
      },
    };
    return () => {
      delete window.jarvisNative;
    };
  }, [busy]);

  useEffect(() => {
    const timer = setTimeout(() => setBooting(false), BOOT_MS);
    return () => clearTimeout(timer);
  }, []);

  // Keys: 1/2/3 switch screens (2 again returns to the question box), Cmd+Enter asks for a
  // suggestion, Escape leaves a text box.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      const modifier = event.metaKey || event.ctrlKey;
      if (modifier && event.key === "Enter") {
        if (mode !== "meeting" || !inMeeting) return;
        event.preventDefault();
        suggest();
      } else if (event.key === "Escape") {
        // In a text box Escape leaves the box; anywhere else it closes the panel.
        if (target?.matches(TYPING)) target.blur();
        else if (PANEL) hidePanel();
      } else if (!modifier && !event.altKey && !target?.matches(TYPING)) {
        const next = MODES[Number(event.key) - 1];
        if (!next) return;
        event.preventDefault(); // or the digit would land in a text box the new screen focuses
        if (next === mode) document.querySelector<HTMLElement>(".ask-input")?.focus();
        else setMode(next);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, inMeeting, suggest]);

  return (
    <MotionConfig reducedMotion="user" transition={reduced ? { duration: 0 } : { duration: 0.22, ease: "easeOut" }}>
      <div className="app" data-booting={booting || undefined} data-panel={PANEL || undefined}>
        <header className="bar">
          <span className="brand">Jarvis</span>
          <nav className="tabs" role="tablist">
            {MODES.map((name, i) => (
              <button key={name} className="tab" role="tab" aria-selected={name === mode} onClick={() => setMode(name)}>
                <kbd>{i + 1}</kbd>
                {name}
                {name === mode && <motion.i layoutId="tab-line" className="tab-line" />}
              </button>
            ))}
          </nav>
          {model.app.app && (
            <button
              className="link auto"
              data-on={model.app.auto || undefined}
              onClick={() => jarvis.setAuto(!model.app.auto)}
              title={
                model.app.auto
                  ? "Listening starts by itself when a call begins. Click to turn that off."
                  : "Automatic listening is off. Click to turn it on."
              }
            >
              Auto
            </button>
          )}
          <span className="link" data-link={jarvis.link}>
            {LINK_LABEL[jarvis.link]}
          </span>
          {PANEL && (
            <button className="link close" onClick={hidePanel} title="Close. Control+Option+J brings Jarvis back." aria-label="Close">
              ✕
            </button>
          )}
        </header>

        <Notices notices={model.app.notices} onReconnect={jarvis.reconnect} />

        <AnimatePresence mode="wait" initial={false}>
          <motion.main
            key={mode}
            className="screen"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, transition: { duration: reduced ? 0 : 0.1 } }}
          >
            {mode === "meeting" && (
              <Meeting model={model} onStart={jarvis.start} onStop={jarvis.stop} onSuggest={suggest} />
            )}
            {mode === "ask" && <Ask ask={ask} />}
            {mode === "today" && <Today api={jarvis.api} fresh={model.fresh} />}
          </motion.main>
        </AnimatePresence>

        <Toast info={model.info} />
      </div>
    </MotionConfig>
  );
}

/** Things that stop Jarvis working by itself, kept on screen until they are dealt with. */
function Notices({ notices, onReconnect }: { notices: Notice[]; onReconnect: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  if (notices.length === 0) return null;
  return (
    <div className="notices">
      {notices.map((notice) => (
        <p key={notice.key} className="notice">
          {notice.text}
          {notice.key === "signin" && (
            <button
              className="btn"
              data-augmented-ui="tl-clip br-clip border"
              disabled={busy}
              onClick={() => {
                setBusy(true);
                void onReconnect().finally(() => setBusy(false));
              }}
            >
              {busy ? "Waiting for the browser…" : "Reconnect"}
            </button>
          )}
        </p>
      ))}
    </div>
  );
}

/** Server notices and failed requests, shown briefly above the bottom edge. Click to dismiss. */
function Toast({ info }: { info: Model["info"] }) {
  const [dismissed, setDismissed] = useState(0);
  useEffect(() => {
    if (!info) return;
    const timer = setTimeout(() => setDismissed(info.at), TOAST_MS);
    return () => clearTimeout(timer);
  }, [info]);
  const visible = info && info.at !== dismissed ? info : null;

  return (
    <AnimatePresence>
      {visible && (
        <motion.button
          key={visible.at}
          className="toast"
          data-warn={visible.warn || undefined}
          onClick={() => setDismissed(visible.at)}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
        >
          {visible.text}
        </motion.button>
      )}
    </AnimatePresence>
  );
}
