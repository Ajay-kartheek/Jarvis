# Jarvis UI

The browser front end for Jarvis: a meeting screen, an ask screen and a today screen in one page.
It talks to the Jarvis server over REST (`/api/...`) and a WebSocket (`/ws`), and the server serves
the built files.

## Run

```sh
npm install
npm run dev      # http://localhost:5173, with /api and /ws proxied to http://127.0.0.1:8765
npm run build    # type-checks, then writes the static site to dist/
npm run preview  # serves dist/ locally to look at the build
```

`dist/` uses relative asset paths, so the server can serve it from `/` as it is.

## Demo mode

Open the page with `?demo=1` (for example `http://localhost:5173/?demo=1`) to play a scripted
meeting on a loop with no server at all. The same demo starts by itself when the socket has not
connected within two seconds, and gives way to live data as soon as the server answers. A `DEMO`
tag in the header is shown the whole time.

## Keys

- `1` `2` `3`: Meeting, Ask, Today. On Ask, `2` again returns to the question box.
- `Cmd+Enter` (or `Ctrl+Enter`): ask for a suggestion now, on the Meeting screen.
- `Esc`: leave a text box, so the number keys switch screens again.

## Layout

- `src/api.ts`: types for every message, the REST client and the reconnecting socket.
- `src/store.ts`: folds server messages into one model; chooses live or demo.
- `src/demo.ts`: the scripted meeting and canned replies.
- `src/screens/`: the three screens. `src/components/`: orb, waveform, frame, tickers.
- `src/styles.css`: all styling, with the colours and fonts as variables at the top.

`three` is held at 0.182: later releases log a deprecation warning through the React 18 line of
`@react-three/fiber`.
