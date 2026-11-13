import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/rajdhani/latin-500.css";
import "@fontsource/rajdhani/latin-600.css";
import "@fontsource/rajdhani/latin-700.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";
import "augmented-ui/augmented-ui.min.css";
import "./styles.css";
import App from "./App";
import { VIEW } from "./bridge";
import { Guard } from "./components/Guard";

document.documentElement.dataset.view = VIEW; // the stylesheet lets the dashboard scroll and keeps the orb still

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Guard fallback={<p className="crash">Jarvis hit a display error. Reload the window to recover.</p>}>
      <App />
    </Guard>
  </StrictMode>,
);
