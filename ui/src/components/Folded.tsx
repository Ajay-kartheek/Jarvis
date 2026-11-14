import { useState } from "react";
import { motion } from "motion/react";
import { useTallerThan } from "../hooks";
import { Markdown } from "../markdown";
import { ChevronIcon } from "./icons";

const FOLD_PX = 84; // about four lines: what shows of a long card until it is opened

interface Props {
  body: string;
  /** How much shows while it is folded, in px. */
  limit?: number;
  className?: string;
}

/** Markdown that shows its first lines and opens to the rest. */
export function Folded({ body, limit = FOLD_PX, className = "md" }: Props) {
  const [open, setOpen] = useState(false);
  const [measure, tall] = useTallerThan(limit);
  return (
    <>
      <motion.div
        className="fold"
        data-tall={tall || undefined}
        data-open={open || undefined}
        initial={false}
        animate={{ height: open || !tall ? "auto" : limit }}
      >
        <div ref={measure} className={className}>
          <Markdown text={body} />
        </div>
      </motion.div>
      {tall && (
        <button className="text-btn more" onClick={() => setOpen(!open)} aria-expanded={open}>
          {open ? "Show less" : "Show more"} <ChevronIcon open={open} />
        </button>
      )}
    </>
  );
}
