import { Navigate, useLocation } from "react-router-dom";

import type { SessionResponse } from "../api/types";
import { BrandMark } from "../components/Brand";
import { LOGIN_TRANSITION_KEY } from "../lib/authTransition";
import authStyles from "../styles/auth.module.css";
import commonStyles from "../styles/common.module.css";

export function LoginPage({ session }: { readonly session: SessionResponse }): React.JSX.Element {
  const location = useLocation();
  if (session.authenticated) {
    return <Navigate to="/" replace />;
  }

  const requestedPath =
    typeof location.state === "object" &&
    location.state !== null &&
    "from" in location.state &&
    typeof location.state.from === "string"
      ? location.state.from
      : "/";
  const returnTo =
    requestedPath === "/" ||
    requestedPath === "/insights" ||
    requestedPath === "/admin" ||
    /^\/records\/[A-Za-z0-9_-]{43}$/.test(requestedPath)
      ? requestedPath
      : "/";
  const startPath = `/api/v1/auth/discord/start?returnTo=${encodeURIComponent(returnTo)}`;

  return (
    <main className={authStyles.loginShell}>
      <div className={commonStyles.backgroundGrid} aria-hidden="true" />
      <section className={authStyles.loginVisual} aria-hidden="true">
        <BrandMark />
        <div className={authStyles.orbit}>
          <span />
          <span />
          <span />
        </div>
      </section>
      <section className={authStyles.loginPanel} aria-labelledby="login-title">
        <h1
          id="login-title"
          className={`${commonStyles.productName} ${authStyles.loginProductName}`}
          aria-label="The Shittim Chest Archive"
          lang="en"
        >
          <span className={commonStyles.productNameLine}>THE SHITTIM</span>
          <span className={commonStyles.productNameLine}>CHEST ARCHIVE</span>
        </h1>
        <p className={`${commonStyles.japaneseText} ${commonStyles.japaneseProse}`}>
          シッテムの箱 議事録閲覧システム
        </p>
        <a
          className={`${commonStyles.primaryButton} ${authStyles.loginAuthButton}`}
          href={startPath}
          lang="en"
          onClick={() => sessionStorage.setItem(LOGIN_TRANSITION_KEY, "pending")}
        >
          AUTHENTICATE
        </a>
        <p
          className={`${authStyles.loginNote} ${commonStyles.japaneseText} ${commonStyles.japaneseProse}`}
        >
          吹雪型JCのつどいサーバの先生であることを認証します。
        </p>
      </section>
    </main>
  );
}
