// View 1: the orb. One screen, no tabs: the orb is the whole point, the line under it says what is
// going on, and what Jarvis has said and made is listed below. During a call the orb shrinks and the
// call takes the stage.

import "./orb.css";
import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { isRunning } from "../api";
import { useVoice } from "../audio";
import { IN_APP, native, openDashboard } from "../bridge";
import { parseTime } from "../format";
import { CallBar, CallHistory, SayCard, ThemLine } from "../components/Call";
import { Cards } from "../components/Cards";
import { Composer } from "../components/Composer";
import { Header, type StatusWord } from "../components/Header";
import { LiveLine, useLive } from "../components/LiveLine";
import { Notices } from "../components/Notices";
import { OrbStage } from "../components/OrbStage";
import { type OrbState, orbStateOf } from "../components/orbState";
import { Toast } from "../components/Toast";
import { Trail } from "../components/Trail";
import type { Jarvis, Link } from "../store";

const BOOT_MS = 1600; // frames draw in slowly during this window, briskly afterwards
const TYPING = "input, textarea, select"; // where Escape leaves the box instead of the panel

const WORD: Record<OrbState, string> = {
  idle: "idle",
  listening: "listening",
  thinking: "working",
  speaking: "speaking",
  call: "call",
};

/** One word for the header: whether the page is in touch with Jarvis, then what Jarvis is doing. */
function statusOf(link: Link, orb: OrbState): StatusWord {
  if (link === "demo") return { word: "demo", tone: "warn" };
  if (link === "offline") return { word: "offline", tone: "warn" };
  if (link === "connecting") return { word: "linking", tone: "dim" };
  return { word: WORD[orb], tone: orb === "idle" ? "idle" : "active" };
}

export default function OrbView({ jarvis }: { jarvis: Jarvis }) {
  const { model, link } = jarvis;
  const { app, turn } = model;
  const voice = useVoice();
  const reduced = Boolean(useReducedMotion());
  const orb = orbStateOf(model, voice);
  const live = useLive(model, voice);
  const inCall = orb === "call";
  const input = useRef<HTMLInputElement>(null);
  const feed = useRef<HTMLDivElement>(null);
  const [booting, setBooting] = useState(true);

  // Nothing below the orb yet: it gets the room. The first thing to read or answer makes it step back.
  const quiet = app.conversation.length === 0 && app.cards.length === 0 && !app.tasks.some(isRunning);
  const mode = inCall ? "call" : quiet ? "hero" : "active";

  useEffect(() => {
    const timer = setTimeout(() => setBooting(false), BOOT_MS);
    return () => clearTimeout(timer);
  }, []);

  // The app calls this each time it brings the panel forward. The shortcut and the menu mean "I want to
  // type"; a held key means "I am about to talk", and must not take the keyboard.
  useEffect(() => {
    window.jarvisNative = {
      onShow: (reason) => {
        if (reason === "hotkey" || reason === "menu") requestAnimationFrame(() => input.current?.focus());
      },
    };
    return () => {
      delete window.jarvisNative;
    };
  }, []);

  // Command+Enter asks for something to say during a call. Escape leaves a text box first, then puts the panel away.
  const { suggest } = jarvis;
  const onCall = app.meeting !== null;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
        if (!onCall) return;
        event.preventDefault();
        void suggest();
      } else if (event.key === "Escape") {
        const target = event.target instanceof HTMLElement ? event.target : null;
        if (target?.matches(TYPING)) target.blur();
        else native("hide");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCall, suggest]);

  // Questions are what Jarvis is waiting on: when they arrive they are brought into view.
  const asking = app.cards.filter((card) => card.kind === "questions").map((card) => card.id).join();
  useEffect(() => {
    const box = feed.current;
    const card = asking ? box?.querySelector('[data-kind="questions"]') : null;
    if (!box || !card) return;
    // Scroll the feed itself: scrollIntoView would move the whole page when the window is very small.
    const top = card.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop - 8;
    box.scrollTo({ top, behavior: reduced ? "auto" : "smooth" });
  }, [asking, reduced]);

  const pending = turn && (turn.phase === "working" || turn.phase === "streaming") ? turn : null;
  // A call is not the place for yesterday's conversation: it shows only what was said to Jarvis since it began.
  const began = app.meeting ? parseTime(app.meeting.started_at) : NaN;
  const lines = inCall ? app.conversation.filter((line) => parseTime(line.at) >= began) : app.conversation;
  const offer = app.call.active && app.status === "idle" && app.meeting === null;

  return (
    <div className="orb-view" data-mode={mode} data-booting={booting || undefined}>
      <Header
        status={statusOf(link, orb)}
        auto={app.settings.auto}
        onAuto={(on) => void jarvis.saveSettings({ auto: on })}
        onDashboard={openDashboard}
        onClose={IN_APP ? () => native("hide") : undefined}
      />

      <Notices notices={app.notices} onReconnect={jarvis.reconnect} />
      <AnimatePresence initial={false}>
        {offer && (
          <motion.div
            key="offer"
            className="notice offer"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
          >
            <span>A call is on{app.call.app ? ` in ${app.call.app}` : ""}.</span>
            <button className="btn" data-augmented-ui="tl-clip br-clip border" onClick={() => void jarvis.start(null, null)}>
              Listen to it
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="spacer" aria-hidden="true" />
      <div className="stage">
        <OrbStage state={orb} compact={inCall} />
        {inCall && <CallBar app={app} onSuggest={() => void suggest()} onStop={jarvis.stop} />}
      </div>
      {!inCall && <LiveLine live={live} />}

      <div className="feed" ref={feed}>
        {inCall && (
          <>
            <ThemLine partial={model.partial} status={app.status} />
            <SayCard item={model.current} />
          </>
        )}
        <Trail lines={lines} pending={inCall ? pending : null} onForget={() => void jarvis.forget()} />
        <Cards
          cards={app.cards}
          tasks={app.tasks}
          onAnswer={jarvis.answer}
          onStopTask={jarvis.stopTask}
          onDismiss={(id) => void jarvis.dismiss(id)}
        />
        {inCall && <CallHistory items={model.history} />}
      </div>

      <Composer
        onSend={(text) => void jarvis.talk(text)}
        sending={jarvis.sending}
        inputRef={input}
        listening={model.hearing.phase === "listening"}
        canTalk={app.voice.hear && !inCall}
        whyNot={inCall ? "A call is being listened to" : "The microphone is not available"}
      />
      <Toast info={model.info} />
    </div>
  );
}
