import { Suspense, lazy, useMemo, useRef, useState } from "react";
import { motion } from "motion/react";
import type { Api, AskReply } from "../api";
import { Frame } from "../components/Frame";
import { Guard } from "../components/Guard";
import type { OrbState } from "../components/Orb";
import { Stopwatch } from "../components/Ticker";
import { useReveal } from "../hooks";
import { Markdown, textLength } from "../markdown";
import type { AskStream } from "../store";

const Orb = lazy(() => import("../components/Orb"));

type Run =
  | { phase: "idle" }
  | { phase: "waiting"; id: string; asked: string; since: number }
  | { phase: "done"; id: string; asked: string; reply: AskReply; streamed: boolean }
  | { phase: "error"; id: string; asked: string; error: string };

/** Question-and-answer state. It lives above the screen so an answer survives switching tabs. */
export function useAsk(api: Api, stream: AskStream) {
  const [question, setQuestion] = useState("");
  const [run, setRun] = useState<Run>({ phase: "idle" });
  const latest = useRef(stream);
  latest.current = stream;

  const submit = async () => {
    const asked = question.trim();
    if (!asked || run.phase === "waiting") return;
    const id = `${Date.now()}`;
    setQuestion("");
    setRun({ phase: "waiting", id, asked, since: Date.now() });
    try {
      const reply = await api.ask(asked, id);
      // If the answer was already on screen as it streamed in, do not type it out a second time.
      const streamed = latest.current.id === id && latest.current.text.length > 0;
      setRun({ phase: "done", id, asked, reply, streamed });
    } catch (error) {
      setRun({ phase: "error", id, asked, error: error instanceof Error ? error.message : String(error) });
    }
  };

  /** Forget the conversation so far: the next question starts from nothing. */
  const reset = () => {
    if (run.phase === "waiting") return;
    setRun({ phase: "idle" });
    api.resetAsk().catch(() => undefined);
  };

  return { question, setQuestion, run, submit, reset, stream };
}

export function Ask({ ask }: { ask: ReturnType<typeof useAsk> }) {
  const { question, setQuestion, run, submit, reset, stream } = ask;
  const reply = run.phase === "done" ? run.reply : null;
  const live = run.phase === "waiting" && stream.id === run.id ? stream : null;
  const total = useMemo(() => textLength(reply?.text ?? ""), [reply]);
  const revealed = useReveal(total, reply, { cps: 80, catchUp: 1.2, instant: reply !== null });
  const shown = run.phase === "done" && run.streamed ? total : revealed;
  const typing = shown < total;
  const orb: OrbState =
    run.phase === "waiting" ? (live?.text ? "speaking" : "thinking") : typing ? "speaking" : question.trim() ? "listening" : "idle";

  return (
    <div className="ask">
      <div className="orb" data-state={orb}>
        {/* If the 3D canvas cannot start (no WebGL) the glow behind it still shows the state. */}
        <Guard>
          <Suspense fallback={null}>
            <Orb state={orb} />
          </Suspense>
        </Guard>
        <motion.span key={orb} className="orb-state" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}>
          {orb}
        </motion.span>
      </div>

      <form
        className="ask-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <input
          className="ask-input"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder={run.phase === "idle" ? "Ask anything" : "Ask a follow-up"}
          aria-label="Question"
          autoFocus
        />
        <button
          className="btn"
          data-augmented-ui="tl-clip br-clip border"
          disabled={run.phase === "waiting" || !question.trim()}
        >
          Ask
        </button>
        {run.phase !== "idle" && (
          <button
            type="button"
            className="btn ghost"
            data-augmented-ui="tl-clip br-clip border"
            onClick={reset}
            disabled={run.phase === "waiting"}
            title="Forget this conversation and start a new one"
          >
            New
          </button>
        )}
      </form>

      {run.phase === "idle" ? (
        <p className="ask-hint">
          Mail, calendar, Drive, project folders, meeting notes and the web. Jarvis works out where to look, and can
          save a reply in your Gmail drafts.
        </p>
      ) : (
        <Frame i={1} className="answer">
          <p className="asked">
            <span className="tag">Q</span>
            {run.asked}
          </p>
          {run.phase === "waiting" && (
            <>
              <div className="thinking">
                {live?.steps.length ? live.steps[live.steps.length - 1] : "Thinking"} <Stopwatch since={run.since} />
              </div>
              {live?.text && (
                <div className="md" data-typing>
                  <Markdown text={live.text} />
                </div>
              )}
            </>
          )}
          {run.phase === "error" && <p className="warn">{run.error}</p>}
          {reply && (
            <>
              {reply.web_queries.length > 0 && (
                <ul className="queries">
                  <li className="label">Searched the web</li>
                  {reply.web_queries.map((query, i) => (
                    <li key={i} className="chip">
                      {query}
                    </li>
                  ))}
                </ul>
              )}
              <div className="md" data-typing={typing || undefined}>
                <Markdown text={reply.text} limit={shown} />
              </div>
              {(reply.drafts ?? []).map((draft, i) => (
                <a key={i} className="draft" href={draft.link} target="_blank" rel="noreferrer">
                  <span className="tag">Draft saved</span>
                  {draft.subject} <small>to {draft.to.join(", ")} · not sent · open Gmail drafts</small>
                </a>
              ))}
              {!typing && <p className="took">Answered in {reply.seconds.toFixed(1)}s</p>}
            </>
          )}
        </Frame>
      )}
    </div>
  );
}
