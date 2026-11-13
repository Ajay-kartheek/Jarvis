# Jarvis UI

The browser front end for Jarvis. One build, two views:

- **The orb** (`/?panel=1`, and plain `/`): one screen with no tabs. The orb is the whole point; under it
  one live line says what is going on (your words as they are recognised, the step Jarvis is on, the
  sentence it is saying, or a hint), then the last turns of the conversation, then what Jarvis put in front of
  you (questions to answer, work in progress, prep sheets, notes, drafts, files). During a call the orb
  shrinks and the call takes the stage: the other side's words, the SAY card, Suggest now and Stop.
- **The dashboard** (`/?view=dashboard`): an ordinary window for reading and managing what Jarvis knows,
  made and did: Overview, Today, Library, Engagements, Activity, Settings.

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
no server at all: a spoken request to prepare for a call, the steps, a questions card, an answer, a prep
card, then a short call with SAY cards, its notes and a draft. It also answers every dashboard endpoint with
made-up data. The same demo starts by itself when the socket has not connected within two seconds, and
gives way to live data as soon as the server answers. The header says `demo` the whole time.

`?demo=1&scene=<name>` holds still on one moment instead, for looking at a single state: `idle`,
`listening`, `thinking`, `speaking`, `questions`, `cards`, `call`, `overview`.

## The app's side

- `window.webkit.messageHandlers.jarvis.postMessage({action})` with `hide`, `show`, `quit`, `dashboard`,
  `voice_start` and `voice_stop`. In a plain browser there is no handler: the close button and the
  microphone button are not shown, and the dashboard button opens `/?view=dashboard` in a new tab.
- `window.jarvisNative.onShow(reason)`: `hotkey` and `menu` focus the text box, `voice` must not take focus.
- `<html>` has the class `jarvis-native` and the variable `--jarvis-titlebar` inside the app. The strip
  of that height at the top of the panel is the window's drag area, and the page leaves it empty.

## Keys

- `Enter` sends a typed message, `Shift+Enter` in an answer box starts a new line.
- `Cmd+Enter` (or `Ctrl+Enter`): ask for a suggestion now, during a call.
- `Esc`: leaves a text box; with none focused it puts the panel away.

## Layout

- `src/api.ts`: types for every message, the REST client and the reconnecting socket.
- `src/store.ts`: folds server messages into one model; chooses live or demo; says which window may speak.
- `src/audio.ts`: plays `speech` messages in order and measures how loud they are. `src/levels.ts`: the
  loudness numbers the orb and the waveform read every frame.
- `src/bridge.ts`: where the page is running and its line to the app.
- `src/demo.ts`, `src/demoData.ts`: the story, its scenes and the made-up data. All names are invented.
- `src/views/OrbView.tsx`, `src/views/Dashboard.tsx` and `src/views/dashboard/`: the two views. Each loads
  on its own, and the 3D library is only fetched by the orb.
- `src/components/`: the orb (`Orb.tsx` is the 3D scene, `OrbStage.tsx` everything around it), the live
  line, conversation, cards, call pieces, composer, header and small shared parts.
- `src/styles.css` holds what both views share, with the colours and fonts as variables at the top;
  `src/views/orb.css` and `src/views/dashboard.css` hold each view's own.

`three` is held at 0.182: later releases log a deprecation warning through the React 18 line of
`@react-three/fiber`.
