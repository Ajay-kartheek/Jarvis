// View 1: the orb. One screen, no tabs: the orb is the whole point, the line under it says what is
// going on, and then comes what Jarvis said last, in full. Below that, what it needs from the user,
// what it is doing and what it made, and the conversation before. Something that waits for a yes
// stands above all of it. During a call the orb shrinks and the call takes the stage. Inside the
// app the whole panel shrinks to the orb alone when there is nothing to show (see mini.ts).

import "./orb.css";
import { type KeyboardEvent, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "motion/react";
import { isRunning } from "../api";
import { useVoice } from "../audio";
import { IN_APP, SIZED, native, openDashboard } from "../bridge";
import { parseTime } from "../format";
import { readingEnds, useSmallOrb } from "../mini";
import { Answer, latestExchange } from "../components/Answer";
import { Approvals } from "../components/Approval";
import { CallBar, CallHistory, SayCard, ThemLine } from "../components/Call";
import { Cards } from "../components/Cards";
import { Composer } from "../components/Composer";
import { Header, type StatusWord } from "../components/Header";
import { LiveLine, useLive } from "../components/LiveLine";
import { MoreBelow, useMoreBelow } from "../components/MoreBelow";
import { Notices } from "../components/Notices";
import { OrbStage } from "../components/OrbStage";
import { type OrbState, orbStateOf } from "../components/orbState";
import { Toast } from "../components/Toast";
import { History, Trail } from "../components/Trail";
import type { Jarvis, Link } from "../store";

const BOOT_MS = 1600; // frames draw in slowly during this window, briskly afterwards
const TYPING = "input, textarea, select"; // where Escape leaves the box instead of the panel

const WORD: Record<OrbState, string> = {
  idle: "idle",
  ready: "ready",
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
  return { word: WORD[orb], tone: orb === "idle" || orb === "ready" ? "idle" : "active" };
}

export default function OrbView({ jarvis }: { jarvis: Jarvis }) {
  const { model, link } = jarvis;
  const { app, turn } = model;
  const voice = useVoice();
  const reduced = Boolean(useReducedMotion());
  const orb = orbStateOf(model, voice);
  const live = useLive(model);
  const inCall = orb === "call";
  const input = useRef<HTMLInputElement>(null);
  // The feed says when the answer (during a call: what to say) runs on below what shows: it must never just stop.
  const { area: feed, more, page } = useMoreBelow(".latest, .say");
  const [booting, setBooting] = useState(true);

  const pending = turn && (turn.phase === "working" || turn.phase === "streaming") ? turn : null;
  // During a call the place under the orb is the call's: what is said to Jarvis is listed in the feed.
  const exchange = inCall ? null : latestExchange(model);
  const asking = app.approvals.length > 0;
  const waiting = app.cards.filter((card) => card.kind === "questions" || card.kind === "proposal");
  const unanswered = app.tasks.some((task) => task.status === "waiting" && task.questions.length > 0);

  // Nothing below the orb yet: it gets the room. The first thing to read or answer makes it step back.
  const quiet = app.conversation.length === 0 && app.cards.length === 0 && !app.tasks.some(isRunning) && !asking && !exchange;
  const mode = inCall ? "call" : quiet ? "hero" : "active";

  // The small orb. The panel stays whole while any of this is on screen, and opens for anything new to read.
  const last = app.conversation.length ? app.conversation[app.conversation.length - 1] : null;
  const { mini, open, shrink, pointer } = useSmallOrb({
    busy:
      inCall || asking || waiting.length > 0 || unanswered || pending !== null || app.busy ||
      model.hearing.phase !== "off" || app.ear.awake || voice.speaking,
    things: [
      ...app.cards.map((card) => `card ${card.id} ${card.at}`),
      ...app.notices.map((notice) => `notice ${notice.key} ${notice.text.length}`),
      last ? `said ${last.at} ${last.text.length}` : "",
      turn ? `turn ${turn.id} ${turn.phase === "error" ? "failed" : ""}` : "",
      model.info ? `told ${model.info.at}` : "",
      app.call.active ? "a call" : "",
    ].filter(Boolean),
    // A reply that was only shown takes time to read; one that was spoken has been heard.
    readUntil: turn?.doneAt && turn.speak !== true ? readingEnds(turn.doneAt, turn.reply) : 0,
  });
  const small = useRef(mini);
  small.current = mini;
  const toType = useRef(false);

  useEffect(() => {
    const timer = setTimeout(() => setBooting(false), BOOT_MS);
    return () => clearTimeout(timer);
  }, []);

  // The app calls this each time it brings the panel forward. The shortcut and the menu mean "I want to
  // type": the whole panel, with the keyboard in the box. A held key means "I am about to talk", and
  // must not take the keyboard.
  useEffect(() => {
    window.jarvisNative = {
      onShow: (reason) => {
        if (reason !== "hotkey" && reason !== "menu") return;
        toType.current = small.current; // the box is not there while the panel is small: see below
        if (small.current) open();
        else requestAnimationFrame(() => input.current?.focus());
      },
      onPointer: pointer,
    };
    return () => {
      delete window.jarvisNative;
    };
  }, [open, pointer]);
  useEffect(() => {
    if (mini || !toType.current) return;
    toType.current = false;
    input.current?.focus();
  }, [mini]);

  // Command+Enter asks for something to say during a call. Escape leaves a text box first, then puts the panel away.
  const { suggest } = jarvis;
  const onCall = app.meeting !== null;
  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
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

  // A new exchange stands at the top of the feed: go there, wherever the feed had been scrolled to. Not
  // for the end of an answer that was being read as it came in: that would pull the reader back to its
  // start. (During a call every new line is on top.) What Jarvis is waiting on (questions, a proposal)
  // is brought into view when it arrives, moving the feed no further than it takes, so the answer
  // above it stays if it can.
  const newest = inCall ? (last?.at ?? "") : (exchange?.key ?? "");
  const awaited = waiting.map((card) => card.id).join();
  const seen = useRef({ newest: "", awaited: "" });
  useEffect(() => {
    const box = feed.current;
    const was = seen.current;
    seen.current = { newest, awaited };
    if (!box) return;
    let top = newest !== was.newest ? 0 : box.scrollTop;
    const card = awaited && awaited !== was.awaited ? box.querySelector<HTMLElement>("[data-waiting]") : null;
    if (card) {
      // Where the card is in what scrolls, whatever the feed is scrolled to at this moment.
      const start = card.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
      const end = start + card.offsetHeight;
      if (start < top + 8) top = start - 8;
      else if (end > top + box.clientHeight - 18) top = Math.min(start - 8, end - box.clientHeight + 18);
      // Not through the middle of a line: if the question above the answer would be cut, it goes whole.
      const asked = box.querySelector<HTMLElement>(".asked");
      const under = asked ? asked.getBoundingClientRect().bottom - box.getBoundingClientRect().top + box.scrollTop : 0;
      if (top > 0 && top < under && under <= start - 8) top = under;
    }
    // Scroll the feed itself: scrollIntoView would move the whole page when the window is very small.
    if (Math.abs(top - box.scrollTop) > 1) box.scrollTo({ top, behavior: reduced ? "auto" : "smooth" });
  }, [feed, newest, awaited, reduced]);

  // A call is not the place for yesterday's conversation: it shows only what was said to Jarvis since it began.
  const began = app.meeting ? parseTime(app.meeting.started_at) : NaN;
  const lines = inCall ? app.conversation.filter((line) => parseTime(line.at) >= began) : app.conversation;
  const earlier = exchange ? lines.slice(0, lines.length - exchange.covers) : lines;
  const offer = app.call.active && app.status === "idle" && app.meeting === null;
  // The small orb is one button: a click, or Enter, brings the panel back.
  const opener = mini
    ? {
        role: "button",
        tabIndex: 0,
        title: "Open Jarvis",
        onClick: open,
        onKeyDown: (event: KeyboardEvent) => (event.key === "Enter" || event.key === " ") && open(),
      }
    : {};

  return (
    <div className="orb-view" data-mode={mode} data-booting={booting || undefined} data-asking={asking || undefined}>
      <Header
        status={statusOf(link, orb)}
        auto={app.settings.auto}
        onAuto={(on) => void jarvis.saveSettings({ auto: on })}
        onDashboard={openDashboard}
        onSmall={SIZED ? shrink : undefined}
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
      <div className="stage" {...opener}>
        {/* The small orb has no header to say that the service is gone, so the orb says it. */}
        <OrbStage state={orb} compact={inCall && !mini} label={mini && link === "offline" ? "Offline" : undefined} />
        {inCall && <CallBar app={app} onSuggest={() => void suggest()} onStop={jarvis.stop} />}
      </div>
      {/* While a yes is asked for, the card says how to give it, and the line only shows words being heard. */}
      {!inCall && !(asking && live.kind === "hint") && <LiveLine live={live} />}

      <Approvals open={app.approvals} cards={app.cards} onSettle={jarvis.settle} />

      <div className="feed-box">
        <div className="feed" ref={feed}>
          {inCall && (
            <>
              <ThemLine partial={model.partial} status={app.status} />
              <SayCard item={model.current} />
              <Trail lines={lines} pending={pending} onForget={() => void jarvis.forget()} />
            </>
          )}
          {exchange && <Answer key={exchange.key} exchange={exchange} />}
          <Cards
            cards={app.cards}
            tasks={app.tasks}
            onAnswer={jarvis.answer}
            onStopTask={jarvis.stopTask}
            onDismiss={(id) => void jarvis.dismiss(id)}
            onChoose={(card, say) => void jarvis.choose(card, say)}
            sending={jarvis.sending}
            said={exchange?.phase === "done" ? exchange.answer : ""}
          />
          {!inCall && lines.length > 0 && <History lines={earlier} onForget={() => void jarvis.forget()} />}
          {inCall && <CallHistory items={model.history} />}
        </div>
        <MoreBelow show={more} onClick={page} />
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
