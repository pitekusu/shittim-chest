import type { PropsWithChildren } from "react";
import { Link, NavLink } from "react-router-dom";

import type { AvatarRef } from "../api/types";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/layout.module.css";
import type { Theme } from "../theme";
import { Avatar } from "./Avatar";
import { BrandMark, ProductName } from "./Brand";

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="3.5" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4" />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M19.2 15.2A8 8 0 0 1 8.8 4.8 8.2 8.2 0 1 0 19.2 15.2Z" />
    </svg>
  );
}

export function ThemeSwitch({
  theme,
  compact = false,
  onToggle,
}: {
  readonly theme: Theme;
  readonly compact?: boolean;
  readonly onToggle: () => void;
}) {
  const dark = theme === "dark";
  return (
    <button
      className={`${styles.themeSwitch} ${compact ? styles.themeSwitchCompact : ""}`}
      type="button"
      role="switch"
      aria-label="ダークモード"
      aria-checked={dark}
      onClick={onToggle}
    >
      {!compact && (
        <span className={styles.themeSwitchLabel} lang="en">
          DARK MODE
        </span>
      )}
      <span className={styles.themeSwitchTrack} aria-hidden="true">
        <span className={styles.themeSwitchSun}>
          <SunIcon />
        </span>
        <span className={styles.themeSwitchMoon}>
          <MoonIcon />
        </span>
        <span className={styles.themeSwitchThumb} />
      </span>
    </button>
  );
}

export function Layout({
  children,
  displayName,
  avatar,
  onLogout,
  theme,
  onThemeToggle,
}: PropsWithChildren<{
  readonly displayName: string;
  readonly avatar: AvatarRef;
  readonly onLogout: () => void;
  readonly theme: Theme;
  readonly onThemeToggle: () => void;
}>) {
  return (
    <div className={styles.appShell}>
      <div className={commonStyles.backgroundGrid} aria-hidden="true" />
      <aside className={styles.sidebar} aria-label="主要ナビゲーション">
        <Link className={styles.brandLink} to="/">
          <BrandMark compact />
          <ProductName />
        </Link>
        <nav>
          <NavLink
            className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
            to="/"
            end
          >
            議論の記録
          </NavLink>
          <NavLink
            className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
            to="/insights"
          >
            いろいろな記録
          </NavLink>
        </nav>
        <div className={styles.sidebarFooter}>
          <ThemeSwitch theme={theme} onToggle={onThemeToggle} />
          <div className={styles.account}>
            <Avatar avatar={avatar} />
            <span>{displayName}</span>
            <button className={commonStyles.quietButton} type="button" lang="en" onClick={onLogout}>
              LOGOFF
            </button>
          </div>
        </div>
      </aside>
      <main className={styles.mainContent} id="main-content" tabIndex={-1}>
        {children}
      </main>
      <nav className={styles.mobileNav} aria-label="モバイルナビゲーション">
        <NavLink
          className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
          to="/"
          end
        >
          記録
        </NavLink>
        <NavLink
          className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
          to="/insights"
        >
          いろいろ
        </NavLink>
        <ThemeSwitch compact theme={theme} onToggle={onThemeToggle} />
        <button className={styles.mobileLogout} type="button" lang="en" onClick={onLogout}>
          LOGOFF
        </button>
      </nav>
    </div>
  );
}
