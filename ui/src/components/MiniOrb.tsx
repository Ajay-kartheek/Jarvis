// A small stand-in for the orb, for windows that are not the orb's: the glow and the turning
// instrument ring, following the same state.

import { useRef } from "react";
import { HudRing, useRingRate } from "./HudRing";
import { type OrbState, RING_RATE } from "./orbState";

export function MiniOrb({ state }: { state: OrbState }) {
  const box = useRef<HTMLDivElement>(null);
  useRingRate(box, RING_RATE[state]);
  return (
    <div ref={box} className="mini-orb" data-state={state} aria-hidden="true">
      <i className="orb-halo" />
      <HudRing />
    </div>
  );
}
