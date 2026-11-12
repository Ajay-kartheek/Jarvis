import { useEffect, useState } from "react";
import { AnimatePresence, MotionConfig, motion, useReducedMotion } from "motion/react";
import { Ask, useAsk } from "./screens/Ask";
import { Meeting } from "./screens/Meeting";
import { Today } from "./screens/Today";
import { type Link, type Model, useJarvis } from "./store";

const MODES = ["meeting", "ask", "today"] as const;
type Mode = (typeof MODES)[number];

const LINK_LABEL: Record<Link, string> = { connecting: "Linking", live: "Live", offline: "Offline", demo: "Demo" };
const TYPING = 'input:not([type="checkbox"]), textarea, select'; // where a digit is text, not a shortcut
const BOOT_MS = 1600; // frames draw in slowly during this window, briskly afterwards
const TOAST_MS = 6000;

export default function App() {
  const jarvis = useJarvis();
  const ask = useAsk(jarvis.api);
  const reduced = useReducedMotion();
  const [mode, setMode] = useState<Mode>("meeting");
  const [booting, setBooting] = useState(true);
  const { model, suggest } = jarvis;
  const inMeeting = model.app.meeting !== null;

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
        target?.blur();
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
      <div className="app" data-booting={booting || undefined}>
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
          <span className="link" data-link={jarvis.link}>
            {LINK_LABEL[jarvis.link]}
          </span>
        </header>

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
            {mode === "today" && <Today api={jarvis.api} />}
          </motion.main>
        </AnimatePresence>

        <Toast info={model.info} />
      </div>
    </MotionConfig>
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
