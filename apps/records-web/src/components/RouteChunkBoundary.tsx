import { Component, type ErrorInfo, type PropsWithChildren } from "react";

import commonStyles from "../styles/common.module.css";
import routeStyles from "../styles/routeMotion.module.css";

interface RouteChunkBoundaryState {
  readonly failed: boolean;
}

export class RouteChunkBoundary extends Component<PropsWithChildren, RouteChunkBoundaryState> {
  public override state: RouteChunkBoundaryState = { failed: false };

  public static getDerivedStateFromError(): RouteChunkBoundaryState {
    return { failed: true };
  }

  public override componentDidCatch(_error: Error, _info: ErrorInfo): void {
    // The route and exception may contain private content. Keep this boundary content-free.
  }

  public override render(): React.ReactNode {
    if (this.state.failed) {
      return (
        <section
          className={commonStyles.messagePanel}
          data-route-motion-ready=""
          data-route-motion-terminal=""
          role="alert"
        >
          <span className={commonStyles.errorRing} aria-hidden="true">
            !
          </span>
          <h1 tabIndex={-1}>画面を読み込めませんでした</h1>
          <p>通信状態を確認して、ページを再読み込みしてください。</p>
          <button
            className={commonStyles.primaryButton}
            type="button"
            onClick={() => window.location.reload()}
          >
            再読み込み
          </button>
        </section>
      );
    }

    return this.props.children;
  }
}

export function RouteLoadingFallback(): React.JSX.Element {
  return (
    <p className={routeStyles.routeLoading} aria-busy="true" aria-live="polite">
      画面を読み込んでいます。
    </p>
  );
}
