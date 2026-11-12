import { memo, useEffect, useState } from "react";
import type { Api, Today as TodayData } from "../api";
import { Frame } from "../components/Frame";
import { hhmm, longDate, parseTime } from "../format";
import { useNow } from "../hooks";

/** Memoised: it only depends on which server it reads from, not on the meeting stream. */
export const Today = memo(function Today({ api }: { api: Api }) {
  const [data, setData] = useState<TodayData | null>(null);
  const [error, setError] = useState("");
  const now = useNow(30_000);

  useEffect(() => {
    let current = true;
    setError("");
    api.today().then(
      (today) => current && setData(today),
      (reason) => current && setError(reason instanceof Error ? reason.message : String(reason)),
    );
    return () => {
      current = false;
    };
  }, [api]);

  return (
    <div className="today">
      <Frame i={0}>
        <div className="label">Today</div>
        <h1 className="date">{data ? longDate(data.date) : error ? "Unavailable" : "Loading…"}</h1>
        {error && <p className="warn">{error}</p>}
      </Frame>

      <Frame i={1}>
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

      <Frame i={2}>
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
