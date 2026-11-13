// View 2: the dashboard. An ordinary window for reading and managing what Jarvis knows, made and did.
// Calmer than the orb: the same look, with less moving.

import "./dashboard.css";
import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { orbStateOf } from "../components/orbState";
import { Toast } from "../components/Toast";
import { LIBRARY_KINDS, isRunning } from "../api";
import { useSwap } from "../hooks";
import type { Jarvis } from "../store";
import { Activity } from "./dashboard/Activity";
import { useDashboardData } from "./dashboard/data";
import { Engagements } from "./dashboard/Engagements";
import { type Focus, Library } from "./dashboard/Library";
import { Overview } from "./dashboard/Overview";
import { Rail, SECTIONS, type SectionId } from "./dashboard/Rail";
import { Settings } from "./dashboard/Settings";
import { TodaySection } from "./dashboard/TodaySection";

/** The section named in the address (#library), so a reloaded window comes back to where it was. */
function useSection(): [SectionId, (section: SectionId) => void] {
  const [section, set] = useState<SectionId>(
    () => SECTIONS.find(({ id }) => id === location.hash.slice(1))?.id ?? "overview",
  );
  const choose = useCallback((next: SectionId) => {
    set(next);
    history.replaceState(null, "", `#${next}`);
    window.scrollTo(0, 0);
  }, []);
  return [section, choose];
}

export default function Dashboard({ jarvis }: { jarvis: Jarvis }) {
  const { model } = jarvis;
  const data = useDashboardData(jarvis.api, model, jarvis.warn);
  const [section, choose] = useSection();
  const [focus, setFocus] = useState<Focus | null>(null);
  const swap = useSwap(10);

  useEffect(() => {
    document.title = "Jarvis dashboard";
  }, []);

  const running = model.app.tasks.filter(isRunning).length;
  const { library } = data;
  const badges = {
    overview: running > 0 ? running : model.app.notices.length > 0 ? "!" : undefined,
    library: library ? LIBRARY_KINDS.reduce((n, kind) => n + library[kind].length, 0) : undefined,
    engagements: data.overview?.engagements.length,
  };

  return (
    <div className="dash">
      <Rail
        section={section}
        onChoose={choose}
        badges={badges}
        link={jarvis.link}
        orb={orbStateOf(model, { speaking: false, saying: "" })}
      />
      <main className="dash-main">
        {data.error && (
          <p className="notice" role="alert">
            <span>Jarvis did not answer: {data.error}. What is shown may be out of date.</span>
            <button className="btn" data-augmented-ui="tl-clip br-clip border" onClick={data.refresh}>
              Try again
            </button>
          </p>
        )}
        <AnimatePresence mode="wait" initial={false}>
          <motion.div key={section} className="page" {...swap}>
            {section === "overview" && <Overview jarvis={jarvis} data={data} />}
            {section === "today" && <TodaySection data={data} />}
            {section === "library" && <Library data={data} focus={focus} />}
            {section === "engagements" && (
              <Engagements
                data={data}
                onOpen={(next) => {
                  setFocus(next);
                  choose("library");
                }}
              />
            )}
            {section === "activity" && <Activity data={data} />}
            {section === "settings" && <Settings jarvis={jarvis} />}
          </motion.div>
        </AnimatePresence>
      </main>
      <Toast info={model.info} />
    </div>
  );
}
