# Jarvis

A personal assistant that runs on your own Mac. It keeps a local copy of your Google mail, calendar and Drive, reads the folders of the projects you work on, listens to your calls and suggests what to say, and answers whatever you ask from one box.

It is built to run by itself. After set-up there is nothing to start and no button to press.

## What it does on its own

- **Starts at login** and stays in the menu bar. Control+Option+J shows and hides the panel.
- **Notices a call** (Teams, Zoom, Meet in a browser and others) and starts listening. It picks the engagement from the calendar, or from the conversation when the call was not scheduled.
- **Suggests replies** during the call, a second or two after the other side asks something, using that engagement's mail, meetings, folders and earlier notes.
- **Writes the notes** when the call ends: summary, decisions, action items. Later questions and the next call's brief can use them.
- **Prepares the day**: a morning brief with today's meetings and the mails that are waiting on you.
- **Keeps itself current**: mail, calendar, Drive and project folders are synced every ten minutes.

## What you can ask

One conversation, in the panel's Ask tab. Jarvis decides where to look:

- your mail, calendar, Drive, project folders and meeting notes;
- the public web, through a separate agent that cannot see your data;
- "draft a reply to …" saves a draft in Gmail's Drafts folder for you to review.

## Limits, on purpose

- Read-only against Google, with one exception: saving a mail draft. It never sends mail and never changes calendar or Drive content. A test in `tests/test_no_send.py` fails the build if code that could do so appears.
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
