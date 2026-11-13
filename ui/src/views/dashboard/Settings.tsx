// Settings: the three switches, and what this Mac can do.

import { useState } from "react";
import type { Settings as SettingsShape } from "../../api";
import { Switch } from "../../components/Switch";
import type { Jarvis } from "../../store";
import { PageHead, Panel } from "./parts";

const SWITCHES: { key: keyof SettingsShape; label: string; about: string }[] = [
  {
    key: "auto",
    label: "Auto",
    about: "Start listening by itself when a call begins, and write the notes when it ends.",
  },
  {
    key: "speak",
    label: "Speak",
    about:
      "Answer out loud when you talk to Jarvis. A typed message always gets a written reply, " +
      "and Jarvis never speaks during a call.",
  },
  {
    key: "auto_prep",
    label: "Auto prep",
    about: "Prepare for client meetings ahead of time, without being asked.",
  },
];

export function Settings({ jarvis }: { jarvis: Jarvis }) {
  const { app } = jarvis.model;
  const [forgotten, setForgotten] = useState(false);

  return (
    <>
      <PageHead title="Settings" sub="Changes take effect straight away." />

      <Panel i={0}>
        <ul className="switches">
          {SWITCHES.map(({ key, label, about }) => (
            <li key={key}>
              <div>
                <strong>{label}</strong>
                <p className="quiet">{about}</p>
              </div>
              <Switch on={app.settings[key]} onChange={(on) => void jarvis.saveSettings({ [key]: on })} title={label} />
            </li>
          ))}
        </ul>
      </Panel>

      <Panel i={1} title="This Mac">
        <dl className="facts-list">
          <div>
            <dt>Hearing you</dt>
            <dd data-ok={app.voice.hear || undefined}>{app.voice.hear ? "Available" : "Not available"}</dd>
          </div>
          <div>
            <dt>Speaking</dt>
            <dd data-ok={app.voice.speak || undefined}>{app.voice.speak ? "Available" : "Not available"}</dd>
          </div>
        </dl>
      </Panel>

      <Panel i={2} title="Conversation">
        <div className="row">
          <p className="quiet">Forget what you and Jarvis have said so far. What it remembers about your work stays.</p>
          <button
            className="btn ghost"
            data-augmented-ui="tl-clip br-clip border"
            disabled={forgotten}
            onClick={() => {
              setForgotten(true);
              void jarvis.forget().finally(() => setTimeout(() => setForgotten(false), 1800));
            }}
          >
            {forgotten ? "Forgotten" : "Forget it"}
          </button>
        </div>
      </Panel>
    </>
  );
}
