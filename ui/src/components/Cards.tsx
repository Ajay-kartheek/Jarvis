// What Jarvis puts in front of the user under its answer: questions it needs answered, things it
// noticed and what could be done about them, the work it is doing, and what it made (prep sheets,
// notes, drafts, files). Each card can be dismissed. What waits for a yes is not here: an approval
// has its own place, above everything (see Approval.tsx).

import { type FormEvent, type KeyboardEvent, type ReactNode, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { type Card, type CardKind, type Task, isRunning } from "../api";
import { elapsed, fileUrl, parseTime, tilde } from "../format";
import { useNow, useSwap } from "../hooks";
import { Markdown, safeLink, withoutTitle } from "../markdown";
import { Folded } from "./Folded";
import { Frame } from "./Frame";
import { CloseIcon, OpenIcon } from "./icons";

const KIND: Record<CardKind, string> = {
  questions: "Questions",
  proposal: "Heads up",
  approval: "Needs your yes",
  prep: "Prep",
  note: "Notes",
  draft: "Draft",
  file: "File",
};
const ELSEWHERE: CardKind[] = ["questions", "proposal", "approval"]; // each has a place of its own; every other kind is listed below them

interface Props {
  cards: Card[];
  tasks: Task[];
  onAnswer: (task: string, text: string) => Promise<boolean>;
  onStopTask: (task: string) => Promise<boolean>;
  onDismiss: (card: string) => void;
  /** A press on one of a proposal's answers: `say` goes to Jarvis as a typed message. */
  onChoose: (card: string, say: string) => void;
  /** A typed message is on its way; another waits for its answer. */
  sending: boolean;
  /** What stands under the orb as Jarvis's latest words: a proposal it has just said does not say it twice. */
  said: string;
}

const ENTER = { initial: { opacity: 0, y: 14, scale: 0.98 }, animate: { opacity: 1, y: 0, scale: 1 } };
const LEAVE = { opacity: 0, scale: 0.96, transition: { duration: 0.18 } };

export function Cards({ cards, tasks, onAnswer, onStopTask, onDismiss, onChoose, sending, said }: Props) {
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
  const proposals = cards.filter((card) => card.kind === "proposal");
  const running = tasks.filter((task) => isRunning(task) && !(task.status === "waiting" && task.questions.length > 0));
  const made = cards.filter((card) => !ELSEWHERE.includes(card.kind));
  return (
    <div className="cards">
      <AnimatePresence initial={false} mode="popLayout">
        {/* What waits for the user comes first, and is brought into view when it arrives (data-waiting). */}
        {questions.map((card) => (
          <motion.div key={card.id} data-waiting="" layout {...ENTER} exit={LEAVE}>
            <Shell card={card} onDismiss={card.id.startsWith("ask-") ? undefined : onDismiss}>
              {/* New questions on the same card start from an empty box, not from "Sent". */}
              <Questions key={(card.questions ?? []).join("\n")} card={card} onAnswer={onAnswer} />
            </Shell>
          </motion.div>
        ))}
        {proposals.map((card) => (
          <motion.div key={card.id} data-waiting="" layout {...ENTER} exit={LEAVE}>
            <Shell card={card} onDismiss={onDismiss}>
              <Proposal card={card} onChoose={onChoose} disabled={sending} told={(card.body ?? "").trim() === said.trim()} />
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
        {/* One line; the whole of a long title is there on hover. */}
        <h3 title={card.title}>{card.title}</h3>
        {onDismiss && (
          <button
            className="icon small"
            onClick={() => onDismiss(card.id)}
            title="Dismiss"
            aria-label={`Dismiss ${card.title || KIND[card.kind] || "this card"}`}
          >
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

/** Something Jarvis noticed, and up to three things to do about it. One press answers it. */
function Proposal({ card, onChoose, disabled, told }: { card: Card; onChoose: Props["onChoose"]; disabled: boolean; told: boolean }) {
  const actions = card.actions ?? [];
  return (
    <>
      {!told && card.body && (
        <div className="md noticed">
          <Markdown text={card.body} />
        </div>
      )}
      {actions.length > 0 && (
        <div className="choices">
          {actions.map((action, i) => (
            <button
              key={i}
              className="btn"
              data-augmented-ui="tl-clip br-clip border"
              disabled={disabled}
              onClick={() => onChoose(card.id, action.say)}
              title={`Tells Jarvis: ${action.say}`}
            >
              {action.label}
            </button>
          ))}
        </div>
      )}
    </>
  );
}

/**
 * Where a piece of work happens. A full path is a link, which the app opens in the Finder. When it
 * does not fit, its end is what shows (`tail`): the folder's own name says more than "~/Documents".
 */
function Folder({ path }: { path: string }) {
  const href = fileUrl(path);
  return href ? (
    <a className="path tail" href={href} target="_blank" rel="noreferrer" title={`Open ${path}`}>
      <bdi>{tilde(path)}</bdi>
    </a>
  ) : (
    <span className="path tail" title={path}>
      <bdi>{path}</bdi>
    </span>
  );
}

/**
 * One piece of work in progress, a preparation or work in a folder: what it is, the latest thing it
 * did, how long it has been at it, and where it works if it has a folder.
 */
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
          <strong title={task.title}>{task.title}</strong>
          <AnimatePresence mode="wait" initial={false}>
            <motion.span key={task.step} className="task-step" title={task.step} {...swap}>
              {task.status === "waiting" ? "Waiting for your answer" : task.step || "Starting"}
            </motion.span>
          </AnimatePresence>
          {task.folder && <Folder path={task.folder} />}
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
