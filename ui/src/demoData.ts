// Made-up data for the demo, and for the dashboard when no server answers. Every name here is invented.
// Times are worked out from now, so "a meeting happening now" is always happening now.

import type { Activity, Counts, Dossier, Engagement, Fact, Item, Library, PromiseEntry, Reminder, SourceStatus, Today } from "./api";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** An ISO time `offset` ms from now. */
export const at = (offset = 0) => new Date(Date.now() + offset).toISOString();
export const daysAgo = (days: number) => at(-days * DAY).slice(0, 10);
export const minutes = (n: number) => n * MINUTE;
export const hours = (n: number) => n * HOUR;

export const ENGAGEMENTS: Engagement[] = [
  { name: "Atlas", people: ["Priya", "Ravi"], folders: 4, files: 128 },
  { name: "Borealis", people: ["Meera"], folders: 2, files: 57 },
];

const BRIEF = `## Today
- 10:00 Atlas weekly sync: have the sign-off status and the rehearsal date ready.
- 11:30 Borealis support review: Meera expects the weekend cover costs.

## Needs your reply
- **Ravi**: asks whether the rehearsal can move to Wednesday.
- **Meera**: wants the support terms confirmed in writing by Friday.

## Carried over
- The Atlas cutover checklist still needs an owner for the rollback step.`;

const NOTES = `## Summary
The migration stays on the 14th, provided the data sign-off lands by Friday. Support cover for Borealis starts the Monday after.

## Decisions
- The rehearsal moves to Wednesday.
- Weekend cover is a phase two item.

## Action items
- Me: send the rehearsal checklist to Ravi (Tuesday)
- Ravi: close the two open sign-off checks (Friday)

## Open questions
- Who owns the rollback step?`;

/** What a note card carries: the daemon puts the title and the date line in front of the notes. */
const TODAY = new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
export const NOTES_WITH_TITLE = `# Atlas weekly sync\n\n${TODAY}, 10:00 · Atlas\n\n${NOTES}`;

export const PREP_ATLAS = `## They will likely raise
- Whether the fourteenth still holds, with two sign-off checks open.
- The rehearsal date: Priya wants Wednesday, Ravi prefers Thursday.

## You need from them
- Ravi to close the two open data checks by Friday.
- Priya to confirm who attends the cutover.

## You owe them
- The rehearsal checklist, promised for Tuesday.
- A written summary of the support terms.

## Decisions to get
- Go or no-go for the fourteenth.
- An owner for the rollback step.

## Watch out
- Weekend cover for the first week is **not agreed** yet.

## Not confirmed
- Whether Borealis joins the rehearsal.`;

const PREP_BOREALIS = `## They will likely raise
- Weekend cover and what it costs.
- Response times for priority one issues.

## You need from them
- Meera to sign off the budget for phase two.

## You owe them
- The support terms in writing, by Friday.

## Watch out
- The four hour response time was agreed for weekdays only.`;

const NOTES_KICKOFF = `## Summary
Borealis starts with weekday support from nine to six. Phase two covers weekends, subject to the budget review.

## Action items
- Me: write up the support terms
- Meera: bring the budget figures to the next call`;

const BRIEF_YESTERDAY = `## Today
- 14:00 Borealis kickoff: agree the support hours.

## Worth knowing
- Priya forwarded the updated migration plan (version 3).`;

export const DRAFT = `Hi Priya,

Thanks for the time today. A short summary of where we landed:

- The fourteenth holds if the data sign-off lands by Friday.
- Ravi closes the two open checks.
- We will confirm the rehearsal for Wednesday.

Best regards`;

const DRAFT_MEERA = `Hi Meera,

As discussed, weekday cover runs from nine to six, with a four hour response time on priority one issues. Weekend cover moves to phase two.

Best regards`;

export const COUNTS: Counts = { mail: 1284, events: 96, files: 412, notes: 7, preps: 5, drafts: 3, created: 9 };

export function demoSources(): SourceStatus[] {
  const account = "you@example.com";
  return [
    { name: "gmail", account, last_run: at(-4 * MINUTE), status: "ok", detail: "" },
    { name: "calendar", account, last_run: at(-4 * MINUTE), status: "ok", detail: "" },
    { name: "drive", account, last_run: at(-4 * MINUTE), status: "ok", detail: "" },
    { name: "folder", account: "Atlas", last_run: at(-4 * MINUTE), status: "ok", detail: "128 files" },
    { name: "folder", account: "Borealis", last_run: at(-4 * MINUTE), status: "ok", detail: "57 files" },
    { name: "gmail", account: "team@example.com", last_run: at(-3 * DAY), status: "error", detail: "Sign-in expired" },
  ];
}

export function demoToday(): Today {
  return {
    date: new Date().toLocaleDateString("en-CA"), // local YYYY-MM-DD
    meetings: [
      {
        title: "Atlas weekly sync",
        start: at(-10 * MINUTE),
        end: at(20 * MINUTE),
        engagement: "Atlas",
        people: ["Priya", "Ravi"],
      },
      {
        title: "Borealis support review",
        start: at(90 * MINUTE),
        end: at(135 * MINUTE),
        engagement: "Borealis",
        people: ["Meera"],
      },
      {
        title: "Atlas cutover rehearsal",
        start: at(165 * MINUTE),
        end: at(225 * MINUTE),
        engagement: "Atlas",
        people: ["Ravi"],
        skipped: true, // Ravi moved it to Thursday
      },
      { title: "Planning hour", start: at(240 * MINUTE), end: at(300 * MINUTE), engagement: null, people: [] },
    ],
    engagements: ENGAGEMENTS,
    brief: { date: daysAgo(0), at: at(-3 * HOUR), text: BRIEF },
    notes: [{ title: "Atlas weekly sync", engagement: "Atlas", at: at(-26 * HOUR), text: NOTES_WITH_TITLE }],
  };
}

export function demoLibrary(): Library {
  const item = (fields: Item): Item => fields;
  return {
    preps: [
      item({
        id: 41,
        kind: "prep",
        title: "Prep: Atlas weekly sync",
        engagement: "Atlas",
        at: at(-40 * MINUTE),
        body: PREP_ATLAS,
        path: "~/Documents/Atlas/prep/atlas-weekly-sync.md",
      }),
      item({
        id: 37,
        kind: "prep",
        title: "Prep: Borealis support review",
        engagement: "Borealis",
        at: at(-2 * DAY),
        body: PREP_BOREALIS,
        path: "~/Documents/Borealis/prep/support-review.md",
      }),
    ],
    notes: [
      item({
        id: 33,
        kind: "notes",
        title: "Atlas weekly sync",
        engagement: "Atlas",
        at: at(-26 * HOUR),
        body: NOTES_WITH_TITLE,
        path: "~/Documents/Notes/atlas-weekly-sync.md",
      }),
      item({
        id: 29,
        kind: "notes",
        title: "Borealis kickoff",
        engagement: "Borealis",
        at: at(-9 * DAY),
        body: NOTES_KICKOFF,
        path: "~/Documents/Notes/borealis-kickoff.md",
      }),
    ],
    briefs: [
      item({ id: 50, kind: "brief", title: "Brief for today", engagement: null, at: at(-3 * HOUR), body: BRIEF }),
      item({
        id: 48,
        kind: "brief",
        title: "Brief for yesterday",
        engagement: null,
        at: at(-DAY - 3 * HOUR),
        body: BRIEF_YESTERDAY,
      }),
    ],
    drafts: [
      item({
        id: 44,
        kind: "draft",
        title: "Summary for Priya",
        engagement: "Atlas",
        at: at(-5 * HOUR),
        body: DRAFT,
        link: "https://example.com/mail/drafts/44",
      }),
      item({
        id: 40,
        kind: "draft",
        title: "Support terms for Meera",
        engagement: "Borealis",
        at: at(-DAY),
        body: DRAFT_MEERA,
        link: "https://example.com/mail/drafts/40",
      }),
    ],
    files: [
      item({
        id: 45,
        kind: "file",
        title: "cutover-checklist.md",
        engagement: "Atlas",
        at: at(-4 * HOUR),
        path: "~/Documents/Atlas/cutover-checklist.md",
      }),
      item({
        id: 31,
        kind: "folder",
        title: "Borealis support terms",
        engagement: "Borealis",
        at: at(-3 * DAY),
        path: "~/Documents/Borealis/support-terms",
      }),
    ],
  };
}

export function demoFacts(): Fact[] {
  return [
    { id: 1, text: "The Atlas cutover rehearsal is on Wednesday at 10:00.", engagement: "Atlas", at: at(-2 * HOUR) },
    { id: 2, text: "Ravi owns the Atlas data sign-off.", engagement: "Atlas", at: at(-DAY) },
    { id: 3, text: "Priya likes a written summary after every call.", engagement: "Atlas", at: at(-6 * DAY) },
    { id: 4, text: "Borealis support cover is weekdays, nine to six.", engagement: "Borealis", at: at(-2 * DAY) },
    { id: 5, text: "Meera reviews the Borealis budget on the first of each month.", engagement: "Borealis", at: at(-12 * DAY) },
    { id: 6, text: "Keep spoken replies to two sentences.", engagement: null, at: at(-20 * DAY) },
  ];
}

export function demoActivity(): Activity[] {
  return [
    { at: at(-6 * MINUTE), kind: "sync", text: "Synced mail, calendar and files" },
    { at: at(-40 * MINUTE), kind: "prep", text: "Prepared for the Atlas weekly sync" },
    { at: at(-52 * MINUTE), kind: "talk", text: "Answered: what is on my plate today?" },
    { at: at(-3 * HOUR), kind: "brief", text: "Wrote the brief for today" },
    { at: at(-5 * HOUR), kind: "draft", text: "Saved a Gmail draft: Summary for Priya" },
    { at: at(-26 * HOUR), kind: "notes", text: "Wrote the notes for the Atlas weekly sync" },
    { at: at(-27 * HOUR), kind: "listen", text: "Listened to the Atlas weekly sync (31 min)" },
    { at: at(-DAY - 4 * HOUR), kind: "draft", text: "Saved a Gmail draft: Support terms for Meera" },
    { at: at(-2 * DAY), kind: "prep", text: "Prepared for the Borealis support review" },
    { at: at(-2 * DAY - 2 * HOUR), kind: "remember", text: "Remembered for Borealis: support cover is weekdays, nine to six" },
    { at: at(-6 * DAY), kind: "remember", text: "Remembered for Atlas: Priya likes a written summary after every call" },
    { at: at(-9 * DAY), kind: "notes", text: "Wrote the notes for the Borealis kickoff" },
  ];
}

/** A day `days` from today at `hour`, as the server writes local times. */
function dayAt(days: number, hour: number): Date {
  const day = new Date();
  day.setDate(day.getDate() + days);
  day.setHours(hour, 0, 0, 0);
  return day;
}

/** One reminder comes as seconds since 1970 and the others as text: the server may send either. */
export function demoReminders(): Reminder[] {
  return [
    { id: 3, due: Math.round((Date.now() + 50 * MINUTE) / 1000), text: "Send the rehearsal checklist to Ravi." },
    { id: 4, due: dayAt(1, 9).toISOString(), text: "Ask Meera for the weekend cover costs." },
    { id: 5, due: dayAt(3, 16).toISOString(), text: "Check that the Atlas data sign-off has landed." },
  ];
}

export function demoPromises(): PromiseEntry[] {
  const day = (days: number) => dayAt(days, 12).toLocaleDateString("en-CA"); // local YYYY-MM-DD
  return [
    { id: 11, engagement: "Atlas", owner: "You", text: "Send the rehearsal checklist to Ravi.", due: day(1), status: "open" },
    { id: 12, engagement: "Atlas", owner: "Ravi", text: "Close the two open sign-off checks.", due: day(3), status: "open" },
    { id: 13, engagement: "Borealis", owner: "You", text: "Confirm the support terms in writing.", due: day(3), status: "open" },
    { id: 14, engagement: "Borealis", owner: "Meera", text: "Bring the budget figures to the next call.", due: null, status: "open" },
    { id: 9, engagement: "Atlas", owner: "Priya", text: "Send the updated migration plan.", due: day(-6), status: "done" },
  ];
}

const DOSSIERS: Record<string, string> = {
  Atlas: `## What it is
Moving the Atlas reporting system to the new platform. The cutover is planned for the fourteenth.

## People
- **Priya**: sponsor. Likes a written summary after every call.
- **Ravi**: data lead. Owns the data sign-off.

## Where things stand
- The fourteenth holds if the data sign-off lands by Friday.
- Two of the five sign-off checks are still open.
- The rehearsal has moved once, and nobody owns the rollback step yet.

## Decisions
- Weekend cover is a phase two item.
- Priya decides go or no-go after the rehearsal.

## Recent work
- Notes of the last weekly sync, and a summary drafted for Priya.
- The cutover checklist, version two.`,
  Borealis: `## What it is
Running support for Borealis after their move: weekday cover first, weekends in a second phase.

## People
- **Meera**: operations lead. Reviews the budget on the first of each month.

## Where things stand
- Weekday cover from nine to six is agreed, with a four hour response on priority one issues.
- Weekend cover waits for the budget review.

## Terms
- The four hour response time holds on weekdays only.`,
};

export function demoDossier(name: string): Dossier {
  return {
    name,
    dossier: DOSSIERS[name] ?? null,
    promises: demoPromises().filter((promise) => promise.engagement === name),
  };
}
