import { Suspense, lazy } from "react";
import { MotionConfig, useReducedMotion } from "motion/react";
import { VIEW } from "./bridge";
import { useJarvis } from "./store";

// Each window loads only its own view: the panel never fetches the dashboard, and the dashboard
// never fetches the 3D library.
const OrbView = lazy(() => import("./views/OrbView"));
const Dashboard = lazy(() => import("./views/Dashboard"));

export default function App() {
  const jarvis = useJarvis(VIEW);
  const reduced = useReducedMotion();
  return (
    <MotionConfig reducedMotion="user" transition={reduced ? { duration: 0 } : { duration: 0.22, ease: "easeOut" }}>
      <Suspense fallback={null}>
        {VIEW === "dashboard" ? <Dashboard jarvis={jarvis} /> : <OrbView jarvis={jarvis} />}
      </Suspense>
    </MotionConfig>
  );
}
