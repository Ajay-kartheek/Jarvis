import type { CSSProperties, ReactNode } from "react";

interface Props {
  /** Position in the draw-in order: frames wipe in one after another. */
  i?: number;
  className?: string;
  children: ReactNode;
}

/**
 * A HUD panel with clipped corners. The outer element carries the glow and the draw-in wipe;
 * they cannot live on the inner one because its clip-path makes the corners.
 */
export function Frame({ i = 0, className = "", children }: Props) {
  return (
    <section className={`frame-wrap ${className}`} style={{ "--i": i } as CSSProperties}>
      <div className="frame" data-augmented-ui="tl-clip br-clip border">
        {children}
      </div>
    </section>
  );
}
