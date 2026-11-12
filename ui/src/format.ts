// Display formatting. The server sends times as strings, so every formatter falls back to the raw text.

const pad = (n: number) => String(n).padStart(2, "0");

/**
 * Milliseconds for an ISO date-time, or NaN. Tolerates what Python prints: a space instead of "T",
 * and microseconds, which not every browser parses.
 */
export const parseTime = (value: string) => Date.parse(value.replace(" ", "T").replace(/(\.\d{3})\d+/, "$1"));

/** Wall-clock time as HH:MM:SS. */
export function clock(ms: number): string {
  const d = new Date(ms);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** A duration as MM:SS, or H:MM:SS once past the hour. */
export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(s / 3600);
  return `${hours ? `${hours}:` : ""}${pad(Math.floor(s / 60) % 60)}:${pad(s % 60)}`;
}

/** HH:MM from an ISO date-time or a bare "HH:MM[:SS]". */
export function hhmm(value: string): string {
  const bare = /^(\d{1,2}):(\d{2})/.exec(value);
  if (bare) return `${pad(Number(bare[1]))}:${bare[2]}`;
  const time = parseTime(value);
  return Number.isNaN(time) ? value : clock(time).slice(0, 5);
}

function formatDate(value: string, options: Intl.DateTimeFormatOptions): string {
  const iso = /^(\d{4})-(\d{2})-(\d{2})(.*)$/.exec(value);
  if (!iso) return value;
  // A date without a time is a local calendar day; Date.parse would read it as UTC midnight.
  const time = iso[4] ? parseTime(value) : new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3])).getTime();
  return Number.isNaN(time) ? value : new Date(time).toLocaleDateString(undefined, options);
}

/** "Thursday 12 November 2026" style. */
export const longDate = (value: string) =>
  formatDate(value, { weekday: "long", day: "numeric", month: "long", year: "numeric" });

/** "12 Nov" style. */
export const shortDate = (value: string) => formatDate(value, { day: "numeric", month: "short" });
