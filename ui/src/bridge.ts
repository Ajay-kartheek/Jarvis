// Where this page is running, and its line to the Jarvis app when it is inside one of the app's windows.

declare global {
  interface Window {
    /**
     * Set by the page, called by the Jarvis app. `onShow`: each time it brings its panel forward.
     * `onPointer`: when the mouse comes onto the panel or leaves it, if the app tells (a window that is
     * not in front is not told by the system, so the page cannot always know by itself).
     */
    jarvisNative?: { onShow?: (reason: string) => void; onPointer?: (inside: boolean) => void };
    webkit?: { messageHandlers?: { jarvis?: { postMessage: (message: unknown) => void } } };
  }
}

export type NativeAction = "hide" | "show" | "quit" | "dashboard" | "voice_start" | "voice_stop" | "size";
/** The panel's two forms: the whole panel, or a small square that shows only the orb. */
export type PanelSize = "mini" | "full";
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
export function native(action: NativeAction, detail: Record<string, string> = {}): boolean {
  const port = window.webkit?.messageHandlers?.jarvis;
  if (!port) return false;
  port.postMessage({ ...detail, action });
  return true;
}

/** Somebody answers a request for the panel's size: the app, or the demo standing in for it. */
export const SIZED = IN_APP || DEMO;

/**
 * Asks the app for the small orb or the whole panel. The app resizes its window and then says which
 * form the page is in, with the class `jarvis-mini` on <html>: the page never assumes it was heard.
 * In the demo, in a browser, there is no app to answer, so the demo marks the page itself: the window
 * cannot shrink, but the small orb is shown on its own in the middle of it.
 */
export function resize(mode: PanelSize): boolean {
  if (native("size", { mode })) return true;
  if (!DEMO) return false;
  document.documentElement.classList.toggle("jarvis-mini", mode === "mini");
  return true;
}

/** The dashboard is a window of its own in the app, and a tab of its own in a browser. */
export function openDashboard(): void {
  if (native("dashboard")) return;
  window.open(`${location.pathname}?view=dashboard${DEMO ? "&demo=1" : ""}`, "_blank", "noopener");
}
