// What lies ahead, on the overview: the reminders Jarvis will say when their time comes, and the
// promises it has noted, in both directions. Either list is shown only when the service has it.

import type { PromiseEntry, Reminder } from "../../api";
import { dueLabel, timeOf } from "../../format";
import { Empty, Panel } from "./parts";

const DONE_SHOWN = 3; // kept promises are only a reminder of what was settled lately

export function Reminders({ reminders, now, i }: { reminders: Reminder[]; now: number; i: number }) {
  const soonest = [...reminders].sort((a, b) => (timeOf(a.due) || Infinity) - (timeOf(b.due) || Infinity));
  return (
    <Panel i={i} title="Reminders" aside={`${reminders.length} set`}>
      {soonest.length === 0 ? (
        <Empty>None set. Ask Jarvis to remind you of something, and it is listed here until it is said.</Empty>
      ) : (
        <ul className="ahead">
          {soonest.map((reminder) => {
            const due = timeOf(reminder.due);
            return (
              <li key={reminder.id} data-due={(!Number.isNaN(due) && due <= now) || undefined}>
                <time>{dueLabel(reminder.due, now)}</time>
                <span>{reminder.text}</span>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

const isOpen = (promise: PromiseEntry) => promise.status !== "done";

/** The service writes "me" for the user and "them" for the other side when no name is given. */
const ownerLabel = (owner: string) => (/^me$/i.test(owner.trim()) ? "You" : /^them$/i.test(owner.trim()) ? "Them" : owner);

/** One promise: who owes it, what it is, for which engagement, and by when. */
function Owed({ promise, now, engagement }: { promise: PromiseEntry; now: number; engagement: boolean }) {
  const due = timeOf(promise.due);
  const late = isOpen(promise) && !Number.isNaN(due) && due < now - 86_400_000;
  return (
    <li data-done={!isOpen(promise) || undefined}>
      <b>{ownerLabel(promise.owner)}</b>
      <span>{promise.text}</span>
      {engagement && promise.engagement && <span className="chip">{promise.engagement}</span>}
      <time data-late={late || undefined}>{isOpen(promise) ? dueLabel(promise.due, now, true) || "no date" : "done"}</time>
    </li>
  );
}

/** Promises, the open ones first. `engagement` false leaves the engagement's name out: the list is inside it. */
export function PromiseList({ promises, now, engagement = true }: { promises: PromiseEntry[]; now: number; engagement?: boolean }) {
  const open = promises.filter(isOpen);
  const done = promises.filter((promise) => !isOpen(promise)).slice(0, DONE_SHOWN);
  return (
    <ul className="owed">
      {[...open, ...done].map((promise) => (
        <Owed key={promise.id} promise={promise} now={now} engagement={engagement} />
      ))}
    </ul>
  );
}

export function Promises({ promises, now, i }: { promises: PromiseEntry[]; now: number; i: number }) {
  const open = promises.filter(isOpen).length;
  return (
    <Panel i={i} title="Promises" aside={`${open} open`}>
      {promises.length === 0 ? (
        <Empty>None noted yet. Jarvis takes them from your call notes and mail: who owes what, and by when.</Empty>
      ) : (
        <PromiseList promises={promises} now={now} />
      )}
    </Panel>
  );
}
