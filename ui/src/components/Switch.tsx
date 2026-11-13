interface Props {
  on: boolean;
  onChange: (on: boolean) => void;
  /** Visible text beside the switch. Without it, `title` names the switch for assistive technology. */
  label?: string;
  title?: string;
  disabled?: boolean;
}

/** An on/off switch. The knob slides; the colour says which way it is. */
export function Switch({ on, onChange, label, title, disabled }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label ? undefined : title}
      className="switch"
      title={title}
      disabled={disabled}
      onClick={() => onChange(!on)}
    >
      {label && <span className="switch-label">{label}</span>}
      <span className="switch-track">
        <i className="switch-knob" />
      </span>
    </button>
  );
}
