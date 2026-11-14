// Text that does not fit its box must never just stop: where a scrolling area holds something the
// user has to read to the end (an answer, a mail to approve), a small button at its lower edge says
// that there is more, and pages down.

import { type RefObject, useCallback, useEffect, useRef, useState } from "react";
import { useReducedMotion } from "motion/react";
import { ChevronIcon } from "./icons";

const SLACK_PX = 6; // closer to the end than this counts as the end

/**
 * Follows one scrolling area. `more` is true while there is something below what shows. With
 * `until`, only that element inside the area counts: the end of the answer, not of everything
 * that happens to be listed after it.
 */
export function useMoreBelow(until?: string): { area: RefObject<HTMLDivElement>; more: boolean; page: () => void } {
  const area = useRef<HTMLDivElement>(null);
  const [more, setMore] = useState(false);
  const reduced = useReducedMotion();

  useEffect(() => {
    const box = area.current;
    if (!box) return;
    const check = () => {
      const target = until ? box.querySelector<HTMLElement>(until) : null;
      const hidden = until
        ? target !== null && target.getBoundingClientRect().bottom - box.getBoundingClientRect().bottom
        : box.scrollHeight - box.scrollTop - box.clientHeight;
      setMore(hidden !== false && hidden > SLACK_PX);
    };
    check();
    box.addEventListener("scroll", check, { passive: true });
    // The area is resized with the window, and what is in it grows as text streams in or a card opens.
    const sizes = new ResizeObserver(check);
    sizes.observe(box);
    const watch = () => {
      sizes.disconnect();
      sizes.observe(box);
      for (const child of box.children) sizes.observe(child);
      if (until) box.querySelectorAll(until).forEach((target) => sizes.observe(target));
      check();
    };
    watch();
    const content = new MutationObserver(watch);
    content.observe(box, { childList: true });
    return () => {
      box.removeEventListener("scroll", check);
      sizes.disconnect();
      content.disconnect();
    };
  }, [until]);

  /** One page further, or just to the end of what has to be read if that is nearer. */
  const page = useCallback(() => {
    const box = area.current;
    if (!box) return;
    const target = until ? box.querySelector<HTMLElement>(until) : null;
    const left = target
      ? target.getBoundingClientRect().bottom - box.getBoundingClientRect().bottom
      : box.scrollHeight - box.scrollTop - box.clientHeight;
    const step = Math.min(Math.max(40, box.clientHeight - 56), left + 28);
    box.scrollBy({ top: step, behavior: reduced ? "auto" : "smooth" });
  }, [reduced, until]);

  return { area, more, page };
}

export function MoreBelow({ show, onClick }: { show: boolean; onClick: () => void }) {
  if (!show) return null;
  return (
    <button type="button" className="more-below" onClick={onClick} title="There is more below" aria-label="Show more of it">
      More <ChevronIcon open={false} />
    </button>
  );
}
