import { useNow } from "../hooks";

/** Fixed-width readout whose characters flip in as they change. Meant for once-a-second values. */
export function Ticker({ text, className = "" }: { text: string; className?: string }) {
  return (
    <span className={`ticker ${className}`}>
      {[...text].map((char, i) => (
        // Keyed by value: a changed character remounts, which replays its CSS flip.
        <span key={`${i}${char}`}>{char}</span>
      ))}
    </span>
  );
}

/** Seconds since `since`, counting up in tenths while something is pending. */
export function Stopwatch({ since }: { since: number }) {
  const now = useNow(100);
  return <span className="took">{(Math.max(0, now - since) / 1000).toFixed(1)}s</span>;
}
