import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { Model } from "../store";

const TOAST_MS = 6000;

/** Server notices and failed requests, shown briefly above the bottom edge. Click to dismiss. */
export function Toast({ info }: { info: Model["info"] }) {
  const [dismissed, setDismissed] = useState(0);
  useEffect(() => {
    if (!info) return;
    const timer = setTimeout(() => setDismissed(info.at), TOAST_MS);
    return () => clearTimeout(timer);
  }, [info]);
  const visible = info && info.at !== dismissed ? info : null;

  return (
    <AnimatePresence>
      {visible && (
        <motion.button
          key={visible.at}
          className="toast"
          data-warn={visible.warn || undefined}
          onClick={() => setDismissed(visible.at)}
          initial={{ opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0 }}
        >
          {visible.text}
        </motion.button>
      )}
    </AnimatePresence>
  );
}
