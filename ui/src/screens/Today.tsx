import { memo, useEffect, useState } from "react";
import type { Api, Today as TodayData } from "../api";
import { Frame } from "../components/Frame";
import { hhmm, longDate, parseTime, shortDate } from "../format";
import { useNow } from "../hooks";
import { Markdown } from "../markdown";

const REFRESH_MS = 5 * 60_000;
const JUST_MET_MS = 3 * 60 * 60_000;

/**
 * Memoised: it depends on which server it reads from and on `fresh`, which goes up when new notes
 * or a brief arrive, not on the meeting stream.
 */
export const Today = memo(function Today({ api, fresh }: { api: Api; fresh: number }) {
  const [data, setData] = useState<TodayData | null>(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(0); // which note is unfolded
  const now = useNow(30_000);
  const round = Math.floor(now / REFRESH_MS); // read again every few minutes: the calendar moves

  useEffect(() => {
    let current = true;
    api.today().then(
      (today) => {
        if (!current) return;
        setData(today);
        setError("");
      },
      (reason) => current && setError(reason instanceof Error ? reason.message : String(reason)),
    );
    return () => {
      current = false;
    };
  }, [api, fresh, round]);

  const notes = data?.notes ?? [];
  // Straight after a call its notes are what the user came for, so they go to the top.
  const justMet = notes.length > 0 && now - parseTime(notes[0].at) < JUST_MET_MS;
  const notesFrame = notes.length > 0 && (
    <Frame i={3} className="notes">
          <div className="label">Meeting notes</div>
          <ul className="agenda">
            {notes.map((note, i) => (
              <li key={note.at + note.title} className="note">
                <button className="note-head" aria-expanded={open === i} onClick={() => setOpen(open === i ? -1 : i)}>
                  <span className="when">
                    {shortDate(note.at.slice(0, 10))} {hhmm(note.at)}
                  </span>
                  <strong>{note.title}</strong>
                  {note.engagement && <span className="chip">{note.engagement}</span>}
                </button>
                {open === i && (
                  <div className="md">
                    <Markdown text={note.text.replace(/^# .*\n+.*\n+/, "")} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </Frame>
  );
  return (
    <div className="today">
      <Frame i={0}>
        <div className="label">Today</div>
        <h1 className="date">{data ? longDate(data.date) : error ? "Unavailable" : "Loading…"}</h1>
        {error && <p className="warn">{error}</p>}
      </Frame>

      {justMet && notesFrame}

      {data?.brief && (
        <Frame i={1} className="brief">
          <div className="label">Brief · written {hhmm(data.brief.at)}</div>
          <div className="md">
            <Markdown text={data.brief.text} />
          </div>
        </Frame>
      )}

      <Frame i={2}>
        <div className="label">Meetings</div>
        {data?.meetings.length === 0 && <p className="quiet">No meetings today.</p>}
        <ul className="agenda">
          {data?.meetings.map((meeting, i) => (
            <li key={i} data-now={(parseTime(meeting.start) <= now && now < parseTime(meeting.end)) || undefined}>
              <span className="when">
                {hhmm(meeting.start)}–{hhmm(meeting.end)}
              </span>
              <div>
                <strong>{meeting.title}</strong>
                {meeting.engagement && <span className="chip">{meeting.engagement}</span>}
                {meeting.people.length > 0 && <p>{meeting.people.join(" · ")}</p>}
              </div>
            </li>
          ))}
        </ul>
      </Frame>

      {!justMet && notesFrame}

      <Frame i={4}>
        <div className="label">Engagements</div>
        {data?.engagements.length === 0 && <p className="quiet">No engagements set up.</p>}
        <ul className="agenda">
          {data?.engagements.map((engagement) => (
            <li key={engagement.name} className="counted">
              <div>
                <strong>{engagement.name}</strong>
                {engagement.people.length > 0 && <p>{engagement.people.join(" · ")}</p>}
              </div>
              <span className="when">
                {engagement.folders} folders · {engagement.files} files
              </span>
            </li>
          ))}
        </ul>
      </Frame>
    </div>
  );
});
