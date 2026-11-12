import { Suspense, lazy, useMemo, useState } from "react";
import { motion } from "motion/react";
import type { Api, AskReply } from "../api";
import { Frame } from "../components/Frame";
import { Guard } from "../components/Guard";
import type { OrbState } from "../components/Orb";
import { Stopwatch } from "../components/Ticker";
import { useReveal } from "../hooks";
import { Markdown, textLength } from "../markdown";

const Orb = lazy(() => import("../components/Orb"));

type Run =
  | { phase: "idle" }
  | { phase: "waiting"; asked: string; since: number }
  | { phase: "done"; asked: string; reply: AskReply }
  | { phase: "error"; asked: string; error: string };

/** Question-and-answer state. It lives above the screen so an answer survives switching tabs. */
export function useAsk(api: Api) {
  const [question, setQuestion] = useState("");
  const [web, setWeb] = useState(false);
  const [run, setRun] = useState<Run>({ phase: "idle" });

  const submit = async () => {
    const asked = question.trim();
    if (!asked || run.phase === "waiting") return;
    setQuestion("");
    setRun({ phase: "waiting", asked, since: Date.now() });
    try {
      setRun({ phase: "done", asked, reply: await api.ask(asked, web) });
    } catch (error) {
      setRun({ phase: "error", asked, error: error instanceof Error ? error.message : String(error) });
    }
  };

  return { question, setQuestion, web, setWeb, run, submit };
}

export function Ask({ ask }: { ask: ReturnType<typeof useAsk> }) {
  const { question, setQuestion, web, setWeb, run, submit } = ask;
  const reply = run.phase === "done" ? run.reply : null;
  const total = useMemo(() => textLength(reply?.text ?? ""), [reply]);
  const shown = useReveal(total, reply, { cps: 80, catchUp: 1.2, instant: reply !== null });
  const typing = shown < total;
  const orb: OrbState =
    run.phase === "waiting" ? "thinking" : typing ? "speaking" : question.trim() ? "listening" : "idle";

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
          (document.activeElement as HTMLElement | null)?.blur(); // number keys work again while the answer comes in
          void submit();
        }}
      >
        <input
          className="ask-input"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask anything"
          aria-label="Question"
          autoFocus
        />
        <label className="toggle">
          <input type="checkbox" checked={web} onChange={(event) => setWeb(event.target.checked)} />
          Web
        </label>
        <button
          className="btn"
          data-augmented-ui="tl-clip br-clip border"
          disabled={run.phase === "waiting" || !question.trim()}
        >
          Ask
        </button>
      </form>

      {run.phase !== "idle" && (
        <Frame i={1} className="answer">
          <p className="asked">
            <span className="tag">Q</span>
            {run.asked}
          </p>
          {run.phase === "waiting" && (
            <div className="thinking">
              Thinking <Stopwatch since={run.since} />
            </div>
          )}
          {run.phase === "error" && <p className="warn">{run.error}</p>}
          {reply && (
            <>
              {reply.web_queries.length > 0 && (
                <ul className="queries">
                  <li className="label">Searched</li>
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
              {!typing && <p className="took">Answered in {reply.seconds.toFixed(1)}s</p>}
            </>
          )}
        </Frame>
      )}
    </div>
  );
}
