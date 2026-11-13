import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import type { Notice } from "../api";

/** Things that stop Jarvis working by itself, kept on screen until they are dealt with. */
export function Notices({ notices, onReconnect }: { notices: Notice[]; onReconnect: () => Promise<unknown> }) {
  const [busy, setBusy] = useState(false);
  return (
    <AnimatePresence initial={false}>
      {notices.length > 0 && (
        <motion.div
          key="notices"
          className="notices"
          initial={{ opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: "auto" }}
          exit={{ opacity: 0, height: 0 }}
        >
          {notices.map((notice) => (
            <p key={notice.key} className="notice" role="alert">
              <span>{notice.text}</span>
              {notice.key === "signin" && (
                <button
                  className="btn"
                  data-augmented-ui="tl-clip br-clip border"
                  disabled={busy}
                  onClick={() => {
                    setBusy(true);
                    void onReconnect().finally(() => setBusy(false));
                  }}
                >
                  {busy ? "Waiting for the browser…" : "Reconnect"}
                </button>
              )}
            </p>
          ))}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
