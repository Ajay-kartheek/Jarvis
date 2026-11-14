// Overview: whether Jarvis is working, how recently it read your accounts, how much it holds, what
// needs attention, what it is doing now, and what lies ahead (reminders and promises).

import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { type Counts, type SourceStatus, type Task, isRunning } from "../../api";
import { MiniOrb } from "../../components/MiniOrb";
import { Notices } from "../../components/Notices";
import { orbStateOf } from "../../components/orbState";
import { ago, elapsed, parseTime, tilde } from "../../format";
import { useNow } from "../../hooks";
import type { Jarvis } from "../../store";
import { Promises, Reminders } from "./Ahead";
import type { DashboardData } from "./data";
import { Count, Empty, PageHead, Panel } from "./parts";

const COUNT_LABEL: Record<keyof Counts, string> = {
  mail: "Mail",
  events: "Events",
  files: "Drive files",
  notes: "Notes",
  preps: "Prep sheets",
  drafts: "Drafts",
  created: "Files made",
};

const STATUS_WORD = {
  idle: "Standing by",
  preparing: "Getting ready",
  listening: "Listening to a call",
  thinking: "Listening to a call",
};

interface Props {
  jarvis: Jarvis;
  data: DashboardData;
}

export function Overview({ jarvis, data }: Props) {
  const { model } = jarvis;
  const { app } = model;
  const now = useNow(1000);
  const orb = orbStateOf(model, { speaking: false, saying: "" });
  const tasks = [...app.tasks].sort((a, b) => Number(isRunning(b)) - Number(isRunning(a)));
  const counts = data.overview?.counts;
  const started = app.meeting ? parseTime(app.meeting.started_at) : NaN;
  // Each is there only if the service has it: an older one has neither, and then there is no gap either.
  const reminders = Array.isArray(data.reminders) ? data.reminders : null;
  const promises = Array.isArray(data.promises) ? data.promises : null;

  return (
    <>
      <PageHead title="Overview" sub="What Jarvis is doing and what it holds.">
        <button
          className="btn"
          data-augmented-ui="tl-clip br-clip border"
          onClick={() => void jarvis.syncNow()}
          disabled={app.syncing}
        >
          {app.syncing ? "Syncing…" : "Sync now"}
        </button>
      </PageHead>

      <Notices notices={app.notices} onReconnect={jarvis.reconnect} />

      <Panel i={0} className="status-panel">
        <div className="status-main">
          <MiniOrb state={orb} />
          <div>
            <motion.p key={app.status} className="big" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}>
              {app.busy && app.status === "idle" ? "Working" : (STATUS_WORD[app.status] ?? app.status)}
            </motion.p>
            <p className="sub">
              {app.ear.on && !app.meeting && "Listening for “Hey Jarvis” · "}
              {app.syncing ? "Syncing now" : `Last sync ${ago(app.last_sync, now)}`}
              {app.meeting && ` · ${app.meeting.title}${Number.isNaN(started) ? "" : `, ${elapsed(now - started)}`}`}
            </p>
          </div>
        </div>
        <Listening jarvis={jarvis} engagements={app.engagements.map((e) => e.name)} />
      </Panel>

      <Panel i={1} title="Holds">
        <dl className="counts">
          {(Object.keys(COUNT_LABEL) as (keyof Counts)[]).map((key) => (
            <div key={key} className="count">
              <dt>{COUNT_LABEL[key]}</dt>
              <dd>{counts ? <Count value={counts[key]} /> : "–"}</dd>
            </div>
          ))}
        </dl>
      </Panel>

      <Panel i={2} title="Tasks" aside={`${tasks.filter(isRunning).length} running`}>
        {tasks.length === 0 ? (
          <Empty>Nothing today. Ask Jarvis to prepare for a meeting, and it shows here.</Empty>
        ) : (
          <ul className="tasks">
            <AnimatePresence initial={false}>
              {tasks.map((task) => (
                <TaskItem key={task.id} task={task} now={now} onStop={jarvis.stopTask} />
              ))}
            </AnimatePresence>
          </ul>
        )}
      </Panel>

      {(reminders || promises) && (
        <div className="cols" data-one={!(reminders && promises) || undefined}>
          {reminders && <Reminders reminders={reminders} now={now} i={3} />}
          {promises && <Promises promises={promises} now={now} i={4} />}
        </div>
      )}

      <Panel i={5} title="Sources" aside={data.overview ? `${data.overview.sources.length} connected` : undefined}>
        <Sources sources={data.overview?.sources} loaded={data.overview !== null} now={now} />
      </Panel>
    </>
  );
}

/** Starts or stops listening to a call by hand: for a call the app did not notice, or with AUTO off. */
function Listening({ jarvis, engagements }: { jarvis: Jarvis; engagements: string[] }) {
  const { app } = jarvis.model;
  const [project, setProject] = useState("");
  const [headphones, setHeadphones] = useState(false);
  const [busy, setBusy] = useState(false);
  const active = app.meeting !== null || app.status !== "idle";
  const act = (job: Promise<unknown>) => {
    setBusy(true);
    void job.finally(() => setBusy(false));
  };

  if (active) {
    return (
      <div className="listening">
        <span className="quiet">Listening to {app.meeting?.engagement ?? "a call"}.</span>
        <button
          className="btn ghost"
          data-augmented-ui="tl-clip br-clip border"
          disabled={busy}
          onClick={() => act(jarvis.stop())}
        >
          Stop listening
        </button>
      </div>
    );
  }
  return (
    <div className="listening">
      <select value={project} onChange={(event) => setProject(event.target.value)} aria-label="Engagement" className="select">
        <option value="">No engagement</option>
        {engagements.map((name) => (
          <option key={name}>{name}</option>
        ))}
      </select>
      {/* The app knows what the sound is playing through; only a plain browser has to ask. */}
      {!app.app && (
        <label className="check">
          <input type="checkbox" checked={headphones} onChange={(event) => setHeadphones(event.target.checked)} />
          Headphones
        </label>
      )}
      <button
        className="btn"
        data-augmented-ui="tl-clip br-clip border"
        disabled={busy}
        onClick={() => act(jarvis.start(project || null, app.app ? null : headphones))}
      >
        Listen to a call
      </button>
    </div>
  );
}

const TASK_WORD: Record<Task["status"], string> = {
  working: "working",
  waiting: "needs you",
  done: "done",
  failed: "failed",
  stopped: "stopped",
};

function TaskItem({ task, now, onStop }: { task: Task; now: number; onStop: (id: string) => Promise<boolean> }) {
  const [stopping, setStopping] = useState(false);
  const started = parseTime(task.started_at);
  const running = isRunning(task);
  return (
    <motion.li
      layout="position"
      className="task-item"
      data-status={task.status}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
    >
      <i className="dot" />
      <div className="task-main">
        <strong>{task.title}</strong>
        <span className="quiet">
          {task.status === "waiting" ? task.questions[0] ?? "Waiting for your answer" : task.step || "–"}
        </span>
        {task.folder && (
          <span className="path" title={task.folder}>
            {tilde(task.folder)}
          </span>
        )}
      </div>
      {task.engagement && <span className="chip">{task.engagement}</span>}
      <span className="task-state">{TASK_WORD[task.status]}</span>
      <span className="took">{!Number.isNaN(started) && (running ? elapsed(now - started) : ago(task.updated_at, now))}</span>
      {running ? (
        <button
          className="btn ghost small"
          data-augmented-ui="tl-clip br-clip border"
          disabled={stopping}
          onClick={() => {
            setStopping(true);
            void onStop(task.id).then((stopped) => !stopped && setStopping(false));
          }}
        >
          Stop
        </button>
      ) : (
        <span />
      )}
    </motion.li>
  );
}

function Sources({ sources, loaded, now }: { sources: SourceStatus[] | undefined; loaded: boolean; now: number }) {
  if (!loaded) return <Empty>Reading…</Empty>;
  if (!sources || sources.length === 0) return <Empty>No accounts or folders are connected yet.</Empty>;
  return (
    <table className="table">
      <thead>
        <tr>
          <th>Source</th>
          <th>Account</th>
          <th>Last read</th>
          <th>Status</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {sources.map((source, i) => (
          <tr key={`${source.name}:${source.account}:${i}`}>
            <td className="strong">{source.name}</td>
            <td>{source.account}</td>
            <td>{ago(source.last_run, now)}</td>
            <td>
              <span className="chip" data-tone={source.status === "ok" ? "ok" : source.status === "never" ? "dim" : "warn"}>
                {source.status}
              </span>
            </td>
            <td className="quiet">{source.detail || "–"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
