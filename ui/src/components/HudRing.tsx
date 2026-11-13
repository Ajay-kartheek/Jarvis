// The instrument ring around the orb: a thin track, two dashed arcs and a ring of ticks, turning
// slowly. It is plain SVG and CSS, so it still shows when the 3D scene cannot start. The turning
// parts are separate SVG elements, not groups inside one: the browser then turns them on the
// graphics card instead of redrawing the picture sixty times a second, which matters for a panel that
// stays open all day.

import { type RefObject, useEffect } from "react";

const TICKS = Array.from({ length: 60 }, (_, i) => i * 6);

const VIEW_BOX = "-100 -100 200 200";

export function HudRing() {
  return (
    <>
      <svg className="orb-hud" viewBox={VIEW_BOX} aria-hidden="true">
        <circle className="hud-track" r="97" />
      </svg>
      <svg className="orb-hud hud-spin" viewBox={VIEW_BOX} aria-hidden="true">
        <circle className="hud-dash" r="91" />
        <path className="hud-arc" d="M 0 -97 A 97 97 0 0 1 84 -48.5" />
        <path className="hud-arc" d="M 0 97 A 97 97 0 0 1 -84 48.5" />
      </svg>
      <svg className="orb-hud hud-spin hud-back" viewBox={VIEW_BOX} aria-hidden="true">
        {TICKS.map((angle) => (
          <line key={angle} className="hud-tick" y1="-86" y2={angle % 30 ? "-83.5" : "-80"} transform={`rotate(${angle})`} />
        ))}
      </svg>
    </>
  );
}

/** Speeds the ring up and slows it down with the state. The playback rate changes without a jump. */
export function useRingRate(box: RefObject<Element | null>, rate: number): void {
  useEffect(() => {
    box.current?.querySelectorAll(".hud-spin").forEach((ring) => {
      ring.getAnimations().forEach((animation) => {
        // updatePlaybackRate changes the speed without a jump; an engine without it still changes the speed.
        if (animation.updatePlaybackRate) animation.updatePlaybackRate(rate);
        else animation.playbackRate = rate;
      });
    });
  }, [box, rate]);
}
