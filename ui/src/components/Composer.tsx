// The way in for typed messages (Enter sends), and, inside the app, a microphone button that listens
// while it is held down with the mouse.

import { type KeyboardEvent, type PointerEvent, type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { IN_APP, native } from "../bridge";
import { MicIcon, SendIcon } from "./icons";

/** Holding the button is holding the key: the app opens the microphone on `voice_start` and closes it on `voice_stop`. */
function useHold(enabled: boolean) {
  const [held, setHeld] = useState(false);
  const holding = useRef(false);

  const start = useCallback(() => {
    if (!enabled || holding.current) return;
    holding.current = true;
    setHeld(true);
    native("voice_start");
  }, [enabled]);

  const stop = useCallback(() => {
    if (!holding.current) return;
    holding.current = false;
    setHeld(false);
    native("voice_stop");
  }, []);

  // A button that is let go of out of sight (the window loses focus, the panel is put away) still ends the message.
  useEffect(() => {
    const away = () => document.hidden && stop();
    window.addEventListener("blur", stop);
    document.addEventListener("visibilitychange", away);
    return () => {
      window.removeEventListener("blur", stop);
      document.removeEventListener("visibilitychange", away);
      stop();
    };
  }, [stop]);
  useEffect(() => {
    if (!enabled) stop();
  }, [enabled, stop]);

  return { held, start, stop };
}

interface MicProps {
  /** The key is held by anyone: the words are being heard. */
  listening: boolean;
  enabled: boolean;
  why: string;
}

function Mic({ listening, enabled, why }: MicProps) {
  const { held, start, stop } = useHold(enabled);
  const down = (event: PointerEvent<HTMLButtonElement>) => {
    if (event.button !== 0) return;
    try {
      event.currentTarget.setPointerCapture(event.pointerId); // let go anywhere: it still ends here
    } catch {
      // the pointer is already gone; the button still works as long as it is let go over it
    }
    start();
  };
  const press = (event: KeyboardEvent<HTMLButtonElement>) => {
    if ((event.key === " " || event.key === "Enter") && !event.repeat) {
      event.preventDefault();
      start();
    }
  };
  const release = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === " " || event.key === "Enter") stop();
  };
  // The wrapper carries the glow: the button itself is clipped to its corners and would cut it off.
  return (
    <span className="tool" data-held={held || undefined} data-listening={listening || undefined}>
      <button
        type="button"
        className="mic"
        data-augmented-ui="tl-clip br-clip border"
        disabled={!enabled}
        title={enabled ? "Hold to talk" : why}
        aria-label="Hold to talk"
        aria-pressed={held}
        onPointerDown={down}
        onPointerUp={stop}
        onPointerCancel={stop}
        onLostPointerCapture={stop}
        onKeyDown={press}
        onKeyUp={release}
        onContextMenu={(event) => event.preventDefault()}
      >
        <MicIcon />
      </button>
    </span>
  );
}

interface Props {
  onSend: (text: string) => void;
  /** A typed message is on its way; sending another waits for its answer. */
  sending: boolean;
  inputRef: RefObject<HTMLInputElement>;
  listening: boolean;
  /** The microphone can be held: a call is not being followed, and this Mac can hear. */
  canTalk: boolean;
  whyNot: string;
}

export function Composer({ onSend, sending, inputRef, listening, canTalk, whyNot }: Props) {
  const [text, setText] = useState("");
  const ready = text.trim() !== "" && !sending;

  const submit = (event: { preventDefault: () => void }) => {
    event.preventDefault();
    if (!ready) return;
    onSend(text.trim());
    setText("");
  };

  return (
    <form className="composer" onSubmit={submit}>
      <div className="field">
        <div className="field-box" data-augmented-ui="tl-clip br-clip border">
          <input
            ref={inputRef}
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="Type to Jarvis"
            aria-label="Message to Jarvis"
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
          />
        </div>
      </div>
      {IN_APP && <Mic listening={listening} enabled={canTalk} why={whyNot} />}
      <span className="tool">
        <button
          className="send"
          data-augmented-ui="tl-clip br-clip border"
          disabled={!ready}
          title="Send (Enter)"
          aria-label="Send"
        >
          <SendIcon />
        </button>
      </span>
    </form>
  );
}
