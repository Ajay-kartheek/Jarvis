// Where this page is running, and its line to the Jarvis app when it is inside one of the app's windows.

declare global {
  interface Window {
    /** Set by the page, called by the Jarvis app each time it brings its panel forward. */
    jarvisNative?: { onShow?: (reason: string) => void };
    webkit?: { messageHandlers?: { jarvis?: { postMessage: (message: unknown) => void } } };
  }
}

export type NativeAction = "hide" | "show" | "quit" | "dashboard" | "voice_start" | "voice_stop";
export type View = "orb" | "dashboard";

const QUERY = new URLSearchParams(location.search);

/** Which of the two views this window shows. */
export const VIEW: View = QUERY.get("view") === "dashboard" ? "dashboard" : "orb";
/** Play the scripted demo instead of talking to a server. */
export const DEMO = QUERY.get("demo") === "1";
/** One moment of the demo to hold still on, for looking at a single state: ?demo=1&scene=questions */
export const SCENE = QUERY.get("scene");
/** True inside the Jarvis app's own windows. */
export const IN_APP = Boolean(window.webkit?.messageHandlers?.jarvis);

/** Asks the app to do something. Returns false in a plain browser, where there is nobody to ask. */
export function native(action: NativeAction): boolean {
  const port = window.webkit?.messageHandlers?.jarvis;
  if (!port) return false;
  port.postMessage({ action });
  return true;
}

/** The dashboard is a window of its own in the app, and a tab of its own in a browser. */
export function openDashboard(): void {
  if (native("dashboard")) return;
  window.open(`${location.pathname}?view=dashboard${DEMO ? "&demo=1" : ""}`, "_blank", "noopener");
}
