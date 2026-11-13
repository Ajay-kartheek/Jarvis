// Display formatting. The server sends times as strings, so every formatter falls back to the raw text,
// and takes null: a record without a time is shown without one, not as an error.

type Text = string | null | undefined;

const pad = (n: number) => String(n).padStart(2, "0");

/**
 * Milliseconds for an ISO date-time, or NaN. Tolerates what Python prints: a space instead of "T",
 * and microseconds, which not every browser parses.
 */
export const parseTime = (value: Text): number =>
  value ? Date.parse(value.replace(" ", "T").replace(/(\.\d{3})\d+/, "$1")) : NaN;

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
export function hhmm(value: Text): string {
  if (!value) return "";
  const bare = /^(\d{1,2}):(\d{2})/.exec(value);
  if (bare) return `${pad(Number(bare[1]))}:${bare[2]}`;
  const time = parseTime(value);
  return Number.isNaN(time) ? value : clock(time).slice(0, 5);
}

function formatDate(value: Text, options: Intl.DateTimeFormatOptions): string {
  if (!value) return "";
  const iso = /^(\d{4})-(\d{2})-(\d{2})(.*)$/.exec(value);
  if (!iso) return value;
  // A date without a time is a local calendar day; Date.parse would read it as UTC midnight.
  const time = iso[4] ? parseTime(value) : new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3])).getTime();
  return Number.isNaN(time) ? value : new Date(time).toLocaleDateString(undefined, options);
}

/** "Thursday 12 November 2026" style. */
export const longDate = (value: Text) =>
  formatDate(value, { weekday: "long", day: "numeric", month: "long", year: "numeric" });

/** "12 Nov" style. */
export const shortDate = (value: Text) => formatDate(value, { day: "numeric", month: "short" });

/** "12 Nov, 14:05": a day and a time on one line. */
function shortStamp(value: Text): string {
  if (!value) return "";
  const date = shortDate(value);
  const time = /T|\s\d/.test(value) ? hhmm(value) : "";
  return time ? `${date}, ${time}` : date;
}

/** "just now", "4 min ago", "3 h ago", "yesterday", then the date. */
export function ago(value: Text, now = Date.now()): string {
  if (!value) return "never";
  const time = parseTime(value);
  if (Number.isNaN(time)) return value;
  const minutes = (now - time) / 60_000;
  if (minutes < -1) return shortStamp(value); // in the future: a clock difference; show the plain stamp
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.round(minutes)} min ago`;
  if (minutes < 60 * 12) return `${Math.round(minutes / 60)} h ago`;
  const days = Math.round((startOfDay(now) - startOfDay(time)) / 86_400_000);
  if (days === 0) return `today, ${hhmm(value)}`;
  if (days === 1) return "yesterday";
  return shortDate(value);
}

function startOfDay(ms: number): number {
  const d = new Date(ms);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** "Today", "Yesterday" or a long date: the heading for a day's worth of entries. */
export function dayLabel(value: Text, now = Date.now()): string {
  const time = parseTime(value);
  if (Number.isNaN(time)) return value ?? "Earlier";
  const days = Math.round((startOfDay(now) - startOfDay(time)) / 86_400_000);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return longDate(value);
}

/** A key that is the same for every moment of one calendar day. */
export function dayKey(value: Text): string {
  const time = parseTime(value);
  return Number.isNaN(time) ? "?" : String(startOfDay(time));
}

/** A path on this Mac as a link the app can open. Only a full path makes one. */
export const fileUrl = (path: string): string | null =>
  path.startsWith("/") ? `file://${path.split("/").map(encodeURIComponent).join("/")}` : null;
