// The dashboard's side rail: the name with a small orb, the six sections, and how the page is linked.

import type { ReactNode } from "react";
import { motion } from "motion/react";
import { IN_APP, native } from "../../bridge";
import { MiniOrb } from "../../components/MiniOrb";
import type { OrbState } from "../../components/orbState";
import type { Link } from "../../store";

export const SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "today", label: "Today" },
  { id: "library", label: "Library" },
  { id: "engagements", label: "Engagements" },
  { id: "activity", label: "Activity" },
  { id: "settings", label: "Settings" },
] as const;

export type SectionId = (typeof SECTIONS)[number]["id"];

const LINK: Record<Link, string> = { connecting: "Linking", live: "Live", offline: "Offline", demo: "Demo data" };

interface Props {
  section: SectionId;
  onChoose: (section: SectionId) => void;
  /** A small number or mark beside a section's name. */
  badges: Partial<Record<SectionId, ReactNode>>;
  link: Link;
  orb: OrbState;
}

export function Rail({ section, onChoose, badges, link, orb }: Props) {
  return (
    <aside className="rail">
      <div className="rail-brand">
        <MiniOrb state={orb} />
        <span className="brand">Jarvis</span>
      </div>
      <nav aria-label="Sections">
        {SECTIONS.map(({ id, label }, i) => (
          <button key={id} className="rail-item" aria-current={id === section ? "page" : undefined} onClick={() => onChoose(id)}>
            {id === section && <motion.i layoutId="rail-bar" className="rail-bar" />}
            <span className="rail-no">{String(i + 1).padStart(2, "0")}</span>
            <span className="rail-label">{label}</span>
            {badges[id] !== undefined && <span className="rail-badge">{badges[id]}</span>}
          </button>
        ))}
      </nav>
      <div className="rail-foot">
        <span className="status" data-tone={link === "live" ? "active" : link === "connecting" ? "dim" : "warn"}>
          {LINK[link]}
        </span>
        {IN_APP && (
          <button className="text-btn" onClick={() => native("show")} title="Bring the orb panel forward">
            Show the orb
          </button>
        )}
      </div>
    </aside>
  );
}
