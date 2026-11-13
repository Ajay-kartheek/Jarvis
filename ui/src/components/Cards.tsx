// What Jarvis puts in front of the user under the conversation: questions it needs answered, the
// work it is doing, and what it made (prep sheets, notes, drafts, files). Each card can be dismissed.

import { type FormEvent, type KeyboardEvent, type ReactNode, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { type Card, type CardKind, type Task, isRunning } from "../api";
import { elapsed, fileUrl, parseTime } from "../format";
import { useNow, useSwap, useTallerThan } from "../hooks";
import { Markdown, safeLink, withoutTitle } from "../markdown";
import { Frame } from "./Frame";
import { ChevronIcon, CloseIcon, OpenIcon } from "./icons";

const FOLD_PX = 84; // about four lines: what shows of a long card until it is opened
const KIND: Record<CardKind, string> = {
  questions: "Questions",
  prep: "Prep",
  note: "Notes",
  draft: "Draft",
  file: "File",
};

interface Props {
  cards: Card[];
  tasks: Task[];
  onAnswer: (task: string, text: string) => Promise<boolean>;
  onStopTask: (task: string) => Promise<boolean>;
  onDismiss: (card: string) => void;
}

const ENTER = { initial: { opacity: 0, y: 14, scale: 0.98 }, animate: { opacity: 1, y: 0, scale: 1 } };
const LEAVE = { opacity: 0, scale: 0.96, transition: { duration: 0.18 } };

export function Cards({ cards, tasks, onAnswer, onStopTask, onDismiss }: Props) {
  const asked = cards.filter((card) => card.kind === "questions");
  const answerable = new Set(asked.map((card) => card.task));
  // A task waiting for an answer always has its questions on screen. If its card was dismissed, they are
  // shown again from the task, so it is never left waiting with nowhere to answer.
  const orphans: Card[] = tasks
    .filter((task) => task.status === "waiting" && task.questions.length > 0 && !answerable.has(task.id))
    .map((task) => ({
      id: `ask-${task.id}`,
      kind: "questions",
      title: task.title,
      body: "",
      at: task.updated_at,
      task: task.id,
      questions: task.questions,
    }));
  const questions = [...asked, ...orphans];
  const running = tasks.filter((task) => isRunning(task) && !(task.status === "waiting" && task.questions.length > 0));
  const made = cards.filter((card) => card.kind !== "questions");
  return (
    <div className="cards">
      <AnimatePresence initial={false} mode="popLayout">
        {questions.map((card) => (
          <motion.div key={card.id} data-kind="questions" layout {...ENTER} exit={LEAVE}>
            <Shell card={card} onDismiss={card.id.startsWith("ask-") ? undefined : onDismiss}>
              {/* New questions on the same card start from an empty box, not from "Sent". */}
              <Questions key={(card.questions ?? []).join("\n")} card={card} onAnswer={onAnswer} />
            </Shell>
          </motion.div>
        ))}
        {running.map((task) => (
          <motion.div key={task.id} layout {...ENTER} exit={LEAVE}>
            <TaskRow task={task} onStop={onStopTask} />
          </motion.div>
        ))}
        {made.map((card) => (
          <motion.div key={card.id} layout {...ENTER} exit={LEAVE}>
            <Shell card={card} onDismiss={onDismiss}>
              {card.kind === "prep" || card.kind === "note" ? <Folded body={withoutTitle(card.body)} /> : <Made card={card} />}
            </Shell>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}

/** The frame every card shares: its kind, its title, and a way to dismiss it. */
function Shell({ card, onDismiss, children }: { card: Card; onDismiss?: (id: string) => void; children: ReactNode }) {
  return (
    <Frame className="card">
      <header className="card-head">
        <span className="tag" data-kind={card.kind}>
          {KIND[card.kind] ?? card.kind}
        </span>
        <h3>{card.title}</h3>
        {onDismiss && (
          <button className="icon small" onClick={() => onDismiss(card.id)} title="Dismiss" aria-label={`Dismiss ${card.title}`}>
            <CloseIcon />
          </button>
        )}
      </header>
      {children}
    </Frame>
  );
}

/** Questions Jarvis needs answered to carry on: each one listed, and one box for the answer. */
function Questions({ card, onAnswer }: { card: Card; onAnswer: Props["onAnswer"] }) {
  const [text, setText] = useState("");
  const [sent, setSent] = useState<"no" | "sending" | "yes">("no");
  const task = card.task;
  const questions = card.questions ?? [];

  const send = async (event?: FormEvent) => {
    event?.preventDefault();
    const answer = text.trim();
    if (!task || !answer || sent === "sending") return;
    setSent("sending");
    setSent((await onAnswer(task, answer)) ? "yes" : "no");
  };
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter starts a new line; a key that only finishes a composed character is left alone.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void send();
    }
  };

  return (
    <>
      {questions.length > 0 ? (
        <ol className="questions">
          {questions.map((question, i) => (
            <li key={i}>{question}</li>
          ))}
        </ol>
      ) : (
        <div className="md">
          <Markdown text={card.body} />
        </div>
      )}
      {task &&
        (sent === "yes" ? (
          <p className="sent">Sent. Jarvis is carrying on.</p>
        ) : (
          <form className="answer" onSubmit={send}>
            <textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              onKeyDown={onKey}
              rows={2}
              placeholder="Your answer"
              aria-label="Your answer"
              disabled={sent === "sending"}
            />
            <button className="btn" data-augmented-ui="tl-clip br-clip border" disabled={!text.trim() || sent === "sending"}>
              Answer
            </button>
          </form>
        ))}
    </>
  );
}

/** One piece of work in progress: what it is, the latest thing it did, how long it has been at it. */
function TaskRow({ task, onStop }: { task: Task; onStop: Props["onStopTask"] }) {
  const now = useNow(1000);
  const swap = useSwap(5);
  const [stopping, setStopping] = useState(false);
  const started = parseTime(task.started_at);
  return (
    <Frame className="task">
      <div className="task-row" data-status={task.status}>
        <i className="dot" />
        <div className="task-main">
          <strong>{task.title}</strong>
          <AnimatePresence mode="wait" initial={false}>
            <motion.span key={task.step} className="task-step" {...swap}>
              {task.status === "waiting" ? "Waiting for your answer" : task.step || "Starting"}
            </motion.span>
          </AnimatePresence>
        </div>
        {!Number.isNaN(started) && <span className="took">{elapsed(now - started)}</span>}
        <button
          className="btn ghost small"
          data-augmented-ui="tl-clip br-clip border"
          disabled={stopping}
          onClick={() => {
            setStopping(true);
            void onStop(task.id).then((stopped) => !stopped && setStopping(false));
          }}
          aria-label={`Stop ${task.title}`}
        >
          {stopping ? "Stopping" : "Stop"}
        </button>
      </div>
      <i className="task-sweep" />
    </Frame>
  );
}

/** Markdown that shows its first lines and opens to the rest. */
function Folded({ body }: { body: string }) {
  const [open, setOpen] = useState(false);
  const [measure, tall] = useTallerThan(FOLD_PX);
  return (
    <>
      <motion.div
        className="fold"
        data-tall={tall || undefined}
        data-open={open || undefined}
        initial={false}
        animate={{ height: open || !tall ? "auto" : FOLD_PX }}
      >
        <div ref={measure} className="md">
          <Markdown text={body} />
        </div>
      </motion.div>
      {tall && (
        <button className="text-btn more" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Show less" : "Show more"} <ChevronIcon open={open} />
        </button>
      )}
    </>
  );
}

/** Drafts and files: what was made, where it is, and a way to open it. */
function Made({ card }: { card: Card }) {
  const href = safeLink(card.link) ?? (card.path ? fileUrl(card.path) : null);
  const body = card.kind === "file" ? "" : card.body.trim(); // a file card's body is only its path again
  return (
    <>
      {body && <Folded body={body} />}
      {card.path && <p className="path">{card.path}</p>}
      {href && (
        <a className="btn open" data-augmented-ui="tl-clip br-clip border" href={href} target="_blank" rel="noreferrer">
          {card.kind === "draft" ? "Open draft" : "Open"} <OpenIcon />
        </a>
      )}
    </>
  );
}
