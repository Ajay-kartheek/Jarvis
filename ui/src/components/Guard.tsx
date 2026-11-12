import { Component, type ReactNode } from "react";

/** Error boundary: if anything inside fails to render, show `fallback` (or nothing) instead of a blank page. */
export class Guard extends Component<{ fallback?: ReactNode; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? (this.props.fallback ?? null) : this.props.children;
  }
}
