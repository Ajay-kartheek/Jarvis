// Settings: the switches, and what this Mac can do. Most switches are the service's; the one for the
// small orb belongs to the panel alone and is kept on this Mac by the page (see mini.ts).

import { useState } from "react";
import type { Settings as SettingsShape } from "../../api";
import { SIZED } from "../../bridge";
import { Switch } from "../../components/Switch";
import { useSmallOrbSetting } from "../../mini";
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
  {
    key: "wake_word",
    label: "Hey Jarvis",
    about:
      "Listen for “Hey Jarvis” while nothing else is going on, so you can talk without holding the key. " +
      "The listening happens on this Mac, and never during a call.",
  },
  {
    key: "proactive",
    label: "Speak up",
    about:
      "After new mail arrives, tell you when a meeting today changed, something you watch for happened, " +
      "or someone needs an answer today. A few times a day at most, and never during a call.",
  },
];

export function Settings({ jarvis }: { jarvis: Jarvis }) {
  const { app } = jarvis.model;
  const [forgotten, setForgotten] = useState(false);
  const [smallOrb, setSmallOrb] = useSmallOrbSetting();

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
              <Switch on={app.settings[key] === true} onChange={(on) => void jarvis.saveSettings({ [key]: on })} title={label} />
            </li>
          ))}
          <li>
            <div>
              <strong>Small orb</strong>
              <p className="quiet">
                Shrink the panel to a small orb after 25 seconds with nothing to show. It opens again when there is
                something to read or to answer, and when you click it.
                {/* The setting is kept in this window's own storage, which the app's two windows share and a browser tab does not. */}
                {!SIZED && " Only the panel in the Jarvis app can shrink: in a browser this changes nothing."}
              </p>
            </div>
            <Switch on={smallOrb} onChange={setSmallOrb} title="Small orb" />
          </li>
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
