// Small pieces the dashboard's sections share.

import { type ReactNode, useEffect } from "react";
import { animate, useMotionValue, useReducedMotion, useTransform, motion } from "motion/react";
import { Frame } from "../../components/Frame";

/** A section's heading, with what it is about and the things to press for it. */
export function PageHead({ title, sub, children }: { title: string; sub?: ReactNode; children?: ReactNode }) {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {sub && <p className="sub">{sub}</p>}
      </div>
      {children && <div className="actions">{children}</div>}
    </header>
  );
}

/** A framed block of the page, with a small heading. `i` staggers the order the blocks draw in. */
export function Panel({
  title,
  aside,
  i = 0,
  className = "",
  children,
}: {
  title?: string;
  aside?: ReactNode;
  i?: number;
  className?: string;
  children: ReactNode;
}) {
  return (
    <Frame i={i} className={`panel ${className}`}>
      {(title || aside) && (
        <header className="panel-head">
          <h2 className="label">{title}</h2>
          {aside && <span className="panel-aside">{aside}</span>}
        </header>
      )}
      {children}
    </Frame>
  );
}

/** A number that counts up to its value when it first shows, and again when the value changes. */
export function Count({ value }: { value: number }) {
  const reduced = useReducedMotion();
  const shown = useMotionValue(reduced ? value : 0);
  const text = useTransform(shown, (n) => Math.round(n).toLocaleString());
  useEffect(() => {
    if (reduced) {
      shown.set(value);
      return;
    }
    const run = animate(shown, value, { duration: 0.9, ease: "easeOut" });
    return () => run.stop();
  }, [value, shown, reduced]);
  return <motion.span>{text}</motion.span>;
}

/** A line of text for a section that has nothing to show yet, or could not load. */
export function Empty({ children }: { children: ReactNode }) {
  return <p className="empty">{children}</p>;
}
