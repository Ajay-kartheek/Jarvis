# Jarvis

A personal assistant that runs on your own Mac. It keeps a local copy of your Google mail, calendar and Drive, reads the folders of the projects you work on, listens to your calls and suggests what to say, and answers whatever you ask from one box.

It is built to run by itself. After set-up there is nothing to start and no button to press.

## One assistant, one view

Jarvis is an orb in a small panel. Hold Control+Option+J and talk, or tap it and type. It answers in a sentence or two, aloud when you spoke, and puts anything longer on a card. A separate dashboard shows what it knows, what it made and what it did.

## What it does on its own

- **Starts at login** and stays in the menu bar.
- **Notices a call** (Teams, Zoom, Meet in a browser and others) and starts listening. It picks the engagement from the calendar, or from the conversation when the call was not scheduled.
- **Suggests replies** during the call, a second or two after the other side asks something, using that engagement's mail, meetings, folders, earlier notes and the preparation sheet.
- **Writes the notes** when the call ends: summary, decisions, action items.
- **Prepares you** before meetings with people from outside your organisation: it reads up, asks you up to three questions, and writes a one-page sheet.
- **Prepares the day**: a morning brief with today's meetings and the mails that are waiting on you.
- **Keeps itself current**: mail, calendar, Drive and project folders are synced every ten minutes.

## What you can ask

- Anything about your work: it searches the local copy of mail, calendar, Drive, engagement folders, notes and preparation sheets.
- Anything public: it sends one short question to a separate agent on the web, which cannot see your data.
- "Get me ready for the Atlas call": a preparation that comes back with questions, then a sheet.
- "Remember that Ravi signs off the budget": kept for good, and used in every later answer and call.
- "Draft a reply to Priya": saved in Gmail's Drafts folder for you to review.
- "For Cedar Labs, create the working folder and set it up for Claude Code": a folder, a git repository and a `CLAUDE.md` brief written from what Jarvis knows, registered as an engagement.

## Limits, on purpose

- Read-only against Google, with two exceptions: saving a mail draft, and sending a draft after you have said yes to that very mail. It never sends on its own and never changes calendar or Drive content. A test in `tests/test_no_send.py` fails the build if any other sending or changing call appears.
- Anything that cannot be undone waits for your yes: a click on the card, or a plain "yes" typed or spoken soon after the question. No model can give that answer for you.
- Work it does by itself in a folder asks before any command beyond plain reading, before writing outside its folder, and before each web search or fetch.
- On the Mac it creates new folders and files in ordinary places under your home folder. It never deletes anything and never overwrites a file it did not write.
- Speech is recognised and spoken on the Mac (Parakeet and Kokoro through MLX). It never speaks while a call is being listened to.
- Audio is recognised on the Mac and never stored. Transcripts are deleted after the number of days set in `config.toml`.
- Model calls go to Claude through the Claude plan login on the Mac. No other AI service is used.
- The agent that reads your mail and files has no internet access; the agent that uses the web cannot see them.

## Set-up

Requirements: an Apple silicon Mac on macOS 14.2 or later, the Xcode Command Line Tools, [uv](https://docs.astral.sh/uv/), Node.js, and Claude Code signed in to a Claude plan.

```
cp config.example.toml config.toml      # then put in your account and projects
uv sync
uv run jarvis auth add you@example.com --client ~/Downloads/client_secret.json
uv run jarvis sync
uv run jarvis project add Atlas --folder ~/Documents/Atlas --person Priya
uv run jarvis install
```

`jarvis auth add` needs an OAuth "Desktop app" client from your own Google Cloud project with the Gmail, Calendar, Drive and Sheets APIs enabled. `jarvis install` builds the app and the interface, copies the app to `~/Applications`, and registers it to start at login. macOS then asks once for the microphone, system audio recording and the Documents folder.

The installed Jarvis runs from a release: a copy of the last commit with its own Python environment, kept next to the data. Editing the project folder does not affect it. `uv run jarvis deploy` moves the release to the current commit.

`uv run jarvis uninstall` removes the app and its login entry and leaves your data alone.

## Where things live

| What | Where |
| --- | --- |
| Settings | `config.toml` in this folder (not in git) |
| Sign-in secrets | macOS Keychain and `~/.config/jarvis/` |
| Local copy, notes, briefs, transcripts | `~/Library/Application Support/Jarvis/` |
| Logs | `~/Library/Logs/Jarvis/` |

## Layout

- `jarvis/` the Python service: mirrors, store, agents, meeting logic, the local server.
- `app/` the macOS app (Swift): menu bar, floating panel, call detection, audio capture.
- `ui/` the interface (React), served by the local server and shown in the app's panel.
- `tests/` run with `uv run pytest`.
