// Today: the date, the brief Jarvis wrote for it, and the day's meetings.

import { motion } from "motion/react";
import { hhmm, longDate, parseTime } from "../../format";
import { useNow } from "../../hooks";
import { Markdown } from "../../markdown";
import type { DashboardData } from "./data";
import { Empty, PageHead, Panel } from "./parts";

export function TodaySection({ data }: { data: DashboardData }) {
  const now = useNow(30_000);
  const today = data.today;
  const brief = today?.brief;

  return (
    <>
      <PageHead title="Today" sub={today ? longDate(today.date) : data.error ? "Unavailable" : "Reading…"} />

      <Panel i={0} title="Brief" aside={brief ? `written ${hhmm(brief.at)}` : undefined}>
        {brief ? (
          <div className="md read">
            <Markdown text={brief.text} />
          </div>
        ) : (
          <Empty>{today ? "No brief yet today. Jarvis writes one in the morning." : "Reading…"}</Empty>
        )}
      </Panel>

      <Panel i={1} title="Meetings" aside={today ? `${today.meetings.length}` : undefined}>
        {today && today.meetings.length === 0 && <Empty>No meetings today.</Empty>}
        <ol className="timeline">
          {today?.meetings.map((meeting, i) => {
            const live = parseTime(meeting.start) <= now && now < parseTime(meeting.end);
            return (
              <motion.li
                key={`${meeting.start}:${meeting.title}`}
                className="meeting"
                data-now={live || undefined}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.05 * i }}
              >
                <span className="when">
                  {hhmm(meeting.start)}
                  <small>{hhmm(meeting.end)}</small>
                </span>
                <div>
                  <strong>{meeting.title}</strong>
                  {meeting.engagement && <span className="chip">{meeting.engagement}</span>}
                  {live && <span className="flag">now</span>}
                  {meeting.people.length > 0 && <p className="quiet">{meeting.people.join(" · ")}</p>}
                </div>
              </motion.li>
            );
          })}
        </ol>
      </Panel>
    </>
  );
}
