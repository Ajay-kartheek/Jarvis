// Something Jarvis will only do after the user's yes: send a mail, run a command, write outside its
// folder, fetch from a new site. While one is open it is the most prominent thing on the panel:
// amber, right under the orb, above everything else. What would be done is shown whole (the mail as
// it would go out, the exact command), scrolling inside the card, so that the buttons never leave
// the screen. It is open exactly as long as `state.approvals` lists it.

import { useState } from "react";
import { type Approval as Question, type Card, TASK_WIDE } from "../api";
import { Markdown } from "../markdown";
import { Frame } from "./Frame";
import { MoreBelow, useMoreBelow } from "./MoreBelow";

interface Props {
  /** What waits for a yes, oldest first. */
  open: Question[];
  /** The cards on the panel: an approval's card carries the text that is shown. */
  cards: Card[];
  onSettle: (approval: string, approve: boolean, scope?: "task") => Promise<boolean>;
}

type Pressed = "yes" | "task" | "no";
const HEADER = /^[A-Z][A-Za-z-]*:\s/;

/**
 * A mail as the service writes it: the lines of its head (From, To, Subject), an empty line, the
 * text. Null if it does not look like that; it is then shown as it came.
 */
function mailParts(text: string): { head: [name: string, value: string][]; body: string } | null {
  const end = text.indexOf("\n\n");
  const lines = (end < 0 ? text : text.slice(0, end)).split("\n");
  if (!lines.every((line) => HEADER.test(line))) return null;
  const head = lines.map((line): [string, string] => {
    const colon = line.indexOf(":");
    return [line.slice(0, colon), line.slice(colon + 1).trim()];
  });
  return { head, body: end < 0 ? "" : text.slice(end + 2) };
}

/**
 * A mail is shown letter for letter, as it would go out: markdown would swallow an asterisk and hide
 * where a link leads, and this is the text the user is saying yes to.
 */
function Mail({ text }: { text: string }) {
  const parts = mailParts(text);
  if (!parts) return <p className="mail-text">{text}</p>;
  return (
    <>
      <dl className="mail-head">
        {parts.head.map(([name, value], i) => (
          <div key={i}>
            <dt>{name}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      <p className="mail-text">{parts.body}</p>
    </>
  );
}

/** The oldest open approval. The others wait behind it: one decision at a time. */
export function Approvals({ open, cards, onSettle }: Props) {
  const first = open[0];
  if (!first) return null;
  const card = cards.find((item) => item.kind === "approval" && item.approval === first.id);
  // Keyed by the question, so that the next one starts with fresh buttons and from its first line.
  return <Ask key={first.id} question={first} card={card} waiting={open.length - 1} onSettle={onSettle} />;
}

function Ask({ question, card, waiting, onSettle }: { question: Question; card?: Card; waiting: number; onSettle: Props["onSettle"] }) {
  const [pressed, setPressed] = useState<Pressed | null>(null);
  const { area, more, page } = useMoreBelow();
  const body = card?.body || question.detail;
  const press = (which: Pressed) => {
    setPressed(which);
    // On a yes or a no the service closes the question and it leaves the screen. If it could not be
    // told, the buttons come back.
    void onSettle(question.id, which !== "no", which === "task" ? "task" : undefined).then((told) => !told && setPressed(null));
  };
  return (
    <Frame className="approval">
      <header className="approval-head">
        <span className="tag" data-kind="approval">
          Needs your yes
        </span>
        {/* A spoken or typed yes settles a question only while it is the only one open. */}
        <span className="approval-hint">
          {waiting > 0 ? `${waiting} more waiting: answer each with its buttons` : "or say or type “yes” or “no”"}
        </span>
      </header>
      <h3>{card?.title || question.title}</h3>
      <div className="approval-body">
        <div ref={area} className="approval-scroll" tabIndex={0} role="group" aria-label="What would be done" data-kind={question.kind}>
          {question.kind === "send_mail" ? (
            <Mail text={body} />
          ) : (
            <div className="md">
              <Markdown text={body} />
            </div>
          )}
        </div>
        <MoreBelow show={more} onClick={page} />
      </div>
      <div className="approval-actions">
        <button className="btn yes" data-augmented-ui="tl-clip br-clip border" disabled={pressed !== null} onClick={() => press("yes")}>
          {pressed === "yes" ? "Approving" : "Approve"}
        </button>
        {TASK_WIDE.includes(question.kind) && (
          <button
            className="btn"
            data-augmented-ui="tl-clip br-clip border"
            disabled={pressed !== null}
            onClick={() => press("task")}
            title="Yes, and do not ask again for the same kind of thing while this task runs"
          >
            {pressed === "task" ? "Allowing" : "Allow for this task"}
          </button>
        )}
        <button className="btn ghost" data-augmented-ui="tl-clip br-clip border" disabled={pressed !== null} onClick={() => press("no")}>
          {pressed === "no" ? "Cancelling" : "Cancel"}
        </button>
      </div>
    </Frame>
  );
}
