// Small line icons, drawn on a 16 px grid in the current text colour.

import type { ReactNode } from "react";

function Icon({ children, size = 16 }: { children: ReactNode; size?: number }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="square"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

export const DashboardIcon = () => (
  <Icon>
    <rect x="1.7" y="1.7" width="5" height="6" />
    <rect x="9.3" y="1.7" width="5" height="3.4" />
    <rect x="1.7" y="10.3" width="5" height="4" />
    <rect x="9.3" y="7.7" width="5" height="6.6" />
  </Icon>
);

export const CloseIcon = () => (
  <Icon>
    <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" />
  </Icon>
);

export const MicIcon = () => (
  <Icon size={18}>
    <rect x="5.7" y="1.6" width="4.6" height="8" rx="2.3" />
    <path d="M3.2 7.6a4.8 4.8 0 0 0 9.6 0M8 12.4v2" />
  </Icon>
);

export const SendIcon = () => (
  <Icon size={18}>
    <path d="M8 13.5v-11M3.4 7l4.6-4.6L12.6 7" />
  </Icon>
);

export const ChevronIcon = ({ open }: { open: boolean }) => (
  <Icon size={12}>
    <path d={open ? "M3 10l5-5 5 5" : "M3 6l5 5 5-5"} />
  </Icon>
);

export const OpenIcon = () => (
  <Icon size={13}>
    <path d="M6.5 3H3v10h10V9.5M9 2.5h4.5V7M13.5 2.5L7.5 8.5" />
  </Icon>
);
