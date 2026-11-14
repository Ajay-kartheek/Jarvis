import { motion } from "motion/react";
import { CloseIcon, DashboardIcon, SmallIcon } from "./icons";
import { Switch } from "./Switch";

export interface StatusWord {
  word: string;
  /** `active`: something is happening. `warn`: the page is not connected to a live server. */
  tone: "idle" | "active" | "warn" | "dim";
}

interface Props {
  status: StatusWord;
  auto: boolean;
  onAuto: (on: boolean) => void;
  onDashboard: () => void;
  /** Only where the panel can shrink to the small orb. */
  onSmall?: () => void;
  /** Only inside the app, where there is a panel to put away. */
  onClose?: () => void;
}

/** The panel's top line: the name, one word for what is going on, and the controls. */
export function Header({ status, auto, onAuto, onDashboard, onSmall, onClose }: Props) {
  return (
    <header className="bar">
      <span className="brand">Jarvis</span>
      <motion.span
        key={status.word}
        className="status"
        data-tone={status.tone}
        initial={{ opacity: 0, y: -4 }}
        animate={{ opacity: 1, y: 0 }}
      >
        {status.word}
      </motion.span>
      <span className="bar-end">
        <button className="icon" onClick={onDashboard} title="Open the dashboard" aria-label="Open the dashboard">
          <DashboardIcon />
        </button>
        <Switch
          on={auto}
          onChange={onAuto}
          label="Auto"
          title={
            auto
              ? "Listening starts by itself when a call begins. Click to turn that off."
              : "Automatic listening is off. Click to turn it on."
          }
        />
        {onSmall && (
          <button
            className="icon"
            onClick={onSmall}
            title="Make small. Click the small orb to open Jarvis again."
            aria-label="Make small"
          >
            <SmallIcon />
          </button>
        )}
        {onClose && (
          <button
            className="icon"
            onClick={onClose}
            title="Close. Control+Option+J brings Jarvis back."
            aria-label="Close"
          >
            <CloseIcon />
          </button>
        )}
      </span>
    </header>
  );
}
