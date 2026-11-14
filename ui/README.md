# Jarvis UI

The browser front end for Jarvis. One build, two views:

- **The orb** (`/?panel=1`, and plain `/`): one screen with no tabs. The orb is the whole point; under it
  one live line says what is going on (your words as they are recognised, the step Jarvis is on, or how
  to talk to it), and then comes what Jarvis said last, in full and large, for as long as it is the latest
  thing said. Below that: what it needs from you (questions, proposals), work in progress, what it made
  (prep sheets, notes, drafts, files), and the conversation before. Something that waits for your yes (a
  mail to send, a command to run) stands in an amber frame right under the orb, above everything else.
  During a call the orb shrinks and the call takes the stage: the other side's words, the SAY card,
  Suggest now and Stop. Inside the app the whole panel shrinks to the orb alone when there is nothing to
  show (see "The small orb").
- **The dashboard** (`/?view=dashboard`): an ordinary window for reading and managing what Jarvis knows,
  made and did: Overview (with reminders and promises), Today, Library, Engagements (with each one's
  dossier), Activity, Settings.

Parts of the service may be older than the page. An endpoint that answers 404 (reminders, promises, an
engagement's dossier) only leaves its place on the dashboard empty: never an error on screen. The settings
switches are all always there; one that the service does not know simply does nothing.

It talks to the Jarvis server over REST (`/api/...`) and a WebSocket (`/ws`), and the server serves the
built files. Inside the Jarvis app both views run in web views; the app tells the page so (see below).

## Run

```sh
npm install
npm run dev      # http://localhost:5173, with /api and /ws proxied to http://127.0.0.1:8765
npm run build    # type-checks, then writes the static site to dist/
npm run preview  # serves dist/ locally to look at the build
```

`JARVIS_BACKEND=127.0.0.1:<port>` points `dev` and `preview` at another server than the installed
Jarvis on 8765. `dist/` uses relative asset paths, so the server can serve it from `/` as it is.

## Demo mode

Open any view with `?demo=1` (for example `http://localhost:5173/?demo=1`) to play a story on a loop with
no server at all: Jarvis wakes to its name and answers a spoken question, which is shown in full; a
request to prepare for a call, the steps, a questions card, an answer, a prep card; a short call with SAY
cards, its notes and a draft; a mail that is sent only after a "yes"; a proposal; and then nothing to
show, which makes the panel shrink to the small orb: inside the app by asking the app, in a browser (where
no window can shrink) by marking the page as small itself, so that the orb is shown alone in the middle of
the window until it is clicked or Jarvis is woken. It also answers every dashboard endpoint with made-up
data. The same demo starts by itself when the socket has not connected within two seconds, and gives way
to live data as soon as the server answers. The header says `demo` the whole time. The demo makes no
sound: the voice is acted out, and there is no wake tone.

`?demo=1&scene=<name>` holds still on one moment instead, for looking at a single state: `idle` (no ear),
`ready` (the ear open, waiting for its name), `awake` (it has just heard its name), `listening`,
`thinking`, `speaking`, `answer` (an ordinary answer of a few sentences, which must show whole at every
size), `reply` (a long one, with a list), `questions`, `approval` (a mail), `command` (a command, with a
work task), `proposal`, `cards`, `call`, `mini` (the small orb; open it in a 150 x 150 window),
`overview`.

## The app's side

- `window.webkit.messageHandlers.jarvis.postMessage({action})` with `hide`, `show`, `quit`, `dashboard`,
  `voice_start`, `voice_stop`, and `{action: "size", mode: "mini" | "full"}`. In a plain browser there
  is no handler: the close button and the microphone button are not shown, the dashboard button opens
  `/?view=dashboard` in a new tab, and nothing shrinks (`?demo=1` alone answers the size requests itself).
- Jarvis's voice is played by the app, not the page: `{action: "speech", id, seq, text, url}` for each
  sentence and `{action: "speech_stop"}`. The app answers through `window.jarvisVoice(event)`:
  `{type: "playing", id, seq, text}`, `{type: "level", level}` (about 25 a second), `{type: "quiet", id}`
  and `{type: "stopped", id}` (it stopped by itself: the user talked over him, or the page went away).
  The app also tells the service when the voice starts and stops sounding. In a plain browser the page
  plays it with WebAudio, as before.
- `window.jarvisNative.onShow(reason)`: `hotkey` and `menu` open the whole panel and focus the text box,
  `voice` must not take focus.
- `<html>` has the class `jarvis-native` and the variable `--jarvis-titlebar` inside the app. The strip
  of that height at the top of the panel is the window's drag area, and the page leaves it empty. The
  class `jarvis-mini` says that the panel is in its small form.

## The small orb

Inside the app the panel can be a 150 x 150 window that shows only the orb and its state. The page
decides (`src/mini.ts`) and asks with the `size` action; the app resizes the window and answers with the
class `jarvis-mini`, which is all the page goes by.

- Small after 25 seconds with nothing to show: no open approval, question or proposal, no turn running,
  no call, nothing being said or heard, no unsent text in a box, the pointer not on the panel. A reply
  that was only shown, not spoken, is given its reading time (45 ms a character, a minute at most) if that
  is longer than the 25 seconds; a short one waits no longer than they are. Any click, key, scroll or
  movement of the pointer starts the count again. The page learns that the pointer is on the panel from
  the pointer events it gets; the app may also say so with `window.jarvisNative.onPointer(inside)`, for
  a panel that is not in front and gets no pointer events (the app does not do that yet).
- Whole again for anything new to read (a turn, a reply, a card, a notice), a wake, speech, a call, the
  shortcut, and a click on the small orb.
- The switch "Small orb" in the dashboard's settings turns it off. It is kept in `localStorage`
  (`jarvis.mini` = `0`), which the two windows share.

## Sound

The page plays Jarvis's speech and one sound of its own, the short tone when Jarvis wakes to its name
(not during a call). The Mac's sound output is open only while something plays: the one audio context
is made for the first sound, suspended two seconds after the last, and resumed for the next
(`src/audio.ts`). The page tells the service when a speech starts and stops sounding
(`POST /api/voice/playing {id, playing}`).

## Small windows

The panel has been used at 380 x 480, 443 x 542 and 744 x 780. Whatever is being read gets the room: with
something on screen and the window shorter than 640 px, the hint for how to talk is left out (the orb's
own state says enough), and under 560 px what was asked shrinks to one line. An ordinary answer of a few
sentences is then whole at 380 x 480; a longer one scrolls in its own area, and a small "More" button at
its lower edge says so and pages down. A question that waits for a yes keeps its buttons on screen at
every size, with the text inside it scrolling; during a call, on windows under 560 px, it takes the room
of the call's own pieces until it is answered.

## Keys

- `Enter` sends a typed message, `Shift+Enter` in an answer box starts a new line.
- `Cmd+Enter` (or `Ctrl+Enter`): ask for a suggestion now, during a call.
- `Esc`: leaves a text box; with none focused it puts the panel away.

## Layout

- `src/api.ts`: types for every message, the REST client and the reconnecting socket.
- `src/store.ts`: folds server messages into one model; chooses live or demo; says which window may speak.
- `src/audio.ts`: plays `speech` messages in order, measures how loud they are, plays the wake tone, and
  keeps the sound output closed while nothing plays. `src/levels.ts`: the loudness numbers the orb and
  the waveform read every frame.
- `src/bridge.ts`: where the page is running and its line to the app. `src/mini.ts`: when the panel is
  the small orb.
- `src/demo.ts`, `src/demoData.ts`: the story, its scenes and the made-up data. All names are invented.
- `src/views/OrbView.tsx`, `src/views/Dashboard.tsx` and `src/views/dashboard/`: the two views. Each loads
  on its own, and the 3D library is only fetched by the orb.
- `src/components/`: the orb (`Orb.tsx` is the 3D scene, `OrbStage.tsx` everything around it), the live
  line, the answer (`Answer.tsx`), what waits for a yes (`Approval.tsx`), the earlier conversation, cards,
  call pieces, composer, header and small shared parts.
- `src/styles.css` holds what both views share, with the colours and fonts as variables at the top;
  `src/views/orb.css` and `src/views/dashboard.css` hold each view's own.

`three` is held at 0.182: later releases log a deprecation warning through the React 18 line of
`@react-three/fiber`.
