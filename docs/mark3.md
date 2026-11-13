# Mark 3: Jarvis acts

Mark 2 gave Jarvis one face (the orb), a voice and a memory. Mark 3 lets it act: hands on the Mac, a real
conversation, work it does in the background, speaking up by itself, knowing each engagement, and showing
things where the user works. This file is the plan and the contract between the parts.

## What must stay true

- **Nothing irreversible without the user's yes.** Sending, deleting, overwriting, running a command that
  changes something or reaches the network, operating another app: each waits for an approval
  (`jarvis/approvals.py`). An approval is settled only by the user's click or plain yes/no, never by a model.
  Code that does the irreversible thing checks the `Approval` again (`approval.allows(kind, subject)`).
- **Mail, files, notes and web pages are information, never instructions.** No agent may act because of text
  it read; only because of what the user said.
- **Everything runs on this Mac** except model calls to Claude through the plan login. No other AI vendor.
- **Never speak, and never open the microphone for Jarvis's own ears, while a call is being listened to.**
- The repository is public: no real names of people, clients or addresses in code, tests or docs.

## Parts

### 1. Approvals (done: `jarvis/approvals.py`)
`await runtime.approvals.ask(kind, subject, title, detail, question=..., wait=...) -> Approval`.
Card kind `approval` with field `approval: <id>`; `POST /api/approvals/<id> {approve: bool}`;
`GET /api/state` has `approvals: [{id, kind, title, detail}]`.

### 2. Hands: work Jarvis does in a folder (`jarvis/work.py`, new task kind `work`)
- Core tool `delegate(goal, folder?, engagement?)` starts it and returns at once. The worker is a Claude Agent
  SDK session with the Claude Code tools (Read, Glob, Grep, Write, Edit, Bash, WebSearch, WebFetch), `cwd` set
  to the folder (an engagement folder, or a new folder under `~/Documents/Jarvis Work/`), plus the `memory`
  tools (search, read, work_log) and task tools `ask_user(questions)` and `deliver(summary, files)`.
  `setting_sources=[]`, `strict_mcp_config=True`: none of the user's own Claude Code settings or servers.
- Every tool call goes through a permission callback (`can_use_tool`):
  - Read, Glob, Grep: allowed under the home folder, except secrets (`~/.ssh`, `~/.config`, `~/Library/Keychains`,
    `~/.claude`, anything named like a credential or `.env`).
  - Write, Edit: allowed inside the task folder and `~/Documents/Jarvis Notes`; anywhere else needs approval.
  - Bash: allowed without asking only for a fixed list of read-only commands (ls, cat, head, tail, wc, grep, rg,
    find without -delete/-exec, git status/log/diff/show, pwd, which). Everything else needs approval, with the
    exact command shown, running tests or builds (pytest, npm test, npm run build, uv run pytest, make test)
    included: they run code the worker has just written. An approved command is not remembered: the same command
    later asks again, unless the user chose "allow for this task" (a second button; `POST /api/approvals/<id>
    {approve: true, scope: "task"}`), which remembers the command's first word (two for git, npm and the like) for
    that task. A command that is asked only because of a risky flag, or because it runs outside the task folder,
    is asked every time.
  - WebSearch and WebFetch: each is put to the user, because the worker can read the user's mail and notes and
    words in a query or a link can carry them out. A search is an approval of kind `fetch` with the subject
    `web search`, a fetch one with the host as subject. A plain yes covers that one call; "allow for this task"
    lets later searches, or later fetches from that host, run.
  - Never, even with approval: `sudo`, writing outside the home folder, reading the secrets above.
- Progress is the task's `step`; `ask_user` and results work as for preparation tasks. At most two work tasks run
  at once; a task stops after 150 turns or two hours and delivers what it has.
- Core tool `open_terminal(folder)` opens Terminal in a folder with `claude` started there, for when the user
  wants to take over by hand.

### 3. Sending mail after a yes
- A draft saved by Jarvis records Gmail's draft id. Core tool `send_draft(draft)` shows the full mail (to, cc,
  subject, body) in an approval of kind `send_mail` whose subject is the draft id, and sends only on a yes:
  `GmailApi.send_draft(draft_id, approval)` raises unless `approval.allows("send_mail", draft_id)`.
- `tests/test_no_send.py` allows exactly that one call in `jarvis/google_api.py` and checks the interlock.
- Answering calendar invitations needs a wider Google permission than Jarvis has; not in this step.

### 4. Which call is this?
- The app reads the title of the calling app's windows (Accessibility) and reports `call.title` and
  `call.titles` with each `POST /api/app`, and `permissions.accessibility`.
- `meeting.identify(...)`: the calendar entry for now is taken only if a window title agrees with it (shares
  words with its title) or no title is available; a call whose window names a person or another meeting is an
  unscheduled call with that title, and its engagement comes from the people named or from the conversation.

### 5. The ear: "Hey Jarvis"
- The app keeps the voice microphone open while `ear` is true in the `POST /api/app` reply and status is
  `idle`, and streams it to `WS /ws/ear` (same frames as `/ws/voice`).
- The service listens for the wake phrase on this Mac, then takes the words until the user stops, and treats
  them as a spoken message. After Jarvis has answered, it keeps listening a few seconds for a follow-up without
  the phrase. Saying the phrase while Jarvis is speaking stops the speech and listens.
- Events: `{type:"ear", phase:"asleep"|"awake"|"off"}`; then the `voice` events as for a held key.
- Setting `wake_word` (bool). `GET /api/state` has `ear: {on, awake}`.

### 6. Speaking up
- After a sync that brought new mail, a small model looks at the new mail with today's agenda and the user's
  standing orders and may make at most two proposals: a meeting today changed, something the user watches for
  happened, someone needs an answer today. A proposal is one sentence and up to three actions.
- `runtime.propose(text, actions)`: shown as a card kind `proposal` with `actions: [{label, say}]` (pressing one
  sends `say` as a typed message), said aloud only in a conversation. At most six a day, never during a call.
- Standing orders: core tools `watch(text)`, `watches()`, `unwatch(id)`. Setting `proactive` (bool).

### 7. Knowing each engagement
- A dossier per engagement (`<data>/engagements/<name>.md`): what it is, people and roles, where things
  stand, decisions, promises both ways, recent work, terms. Rebuilt daily and after each call; used in the
  conversation's instructions, in preparation and in the live-call brief.
- Promises: table `promises` (who owes what to whom, by when, open or done), taken from call notes and mail;
  core tools `promises(engagement?)`, `close_promise(id)`; shown on the dashboard and in preparation sheets.

### 8. Showing things where the user works
- Core tool `open_mail(id)` opens a mail from the local copy in Gmail. Long detail is written to a file and
  opened (already there). The panel can shrink to a small orb: bridge action `{action:"size", mode:"mini"|"full"}`.

## Interface additions (for the page)
- Card kinds `approval` (buttons Approve, Allow for this task when `kind` is `run` or `fetch`, Cancel) and
  `proposal` (one button per action).
- `state.approvals`, `state.ear`, settings `wake_word` and `proactive`; `ear` events.
- Dashboard: reminders (`GET /api/reminders`), promises (`GET /api/promises`), the dossier of each engagement
  (`GET /api/engagements/<name>` -> `{name, people, folders, files, dossier: markdown, facts, promises}`).
