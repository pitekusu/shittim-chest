import { useEffect, useRef, type PropsWithChildren } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";

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

function NavigationIcon({
  kind,
}: {
  readonly kind: "records" | "insights" | "momotalk" | "memorial";
}) {
  const paths = {
    records: "M5 3h14v18H5zM8 8h8M8 12h8M8 16h5",
    insights: "M4 20h16M7 16V9M12 16V4M17 16v-5",
    momotalk: "M4 4h16v12H9l-5 4zM8 9h8M8 12h5",
    memorial: "M4 4h16v16H4zM4 16l5-5 4 4 3-3 4 4M15 8h.01",
  };
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" className={styles.navigationIcon}>
      <path d={paths[kind]} />
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
      {!compact && <span className={styles.themeSwitchLabel}>ダークモード</span>}
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
  logoutPending = false,
  logoutError = null,
  theme,
  onThemeToggle,
}: PropsWithChildren<{
  readonly displayName: string;
  readonly avatar: AvatarRef;
  readonly onLogout: () => void;
  readonly logoutPending?: boolean;
  readonly logoutError?: string | null;
  readonly theme: Theme;
  readonly onThemeToggle: () => void;
}>) {
  const location = useLocation();
  const accountMenu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const menu = accountMenu.current;
    if (menu) menu.open = false;
  }, [location.pathname]);
  useEffect(() => {
    const closeOutside = (event: PointerEvent) => {
      const menu = accountMenu.current;
      if (menu && event.target instanceof Node && !menu.contains(event.target)) menu.open = false;
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      const menu = accountMenu.current;
      if (event.key === "Escape" && menu?.open) {
        menu.open = false;
        menu.querySelector("summary")?.focus();
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, []);
  return (
    <div className={styles.appShell}>
      <div className={commonStyles.backgroundGrid} aria-hidden="true" />
      <header className={styles.mobileHeader}>
        <Link className={styles.mobileBrand} to="/" aria-label="記録一覧へ">
          <BrandMark compact />
          <ProductName />
        </Link>
        <details className={styles.accountMenu} ref={accountMenu}>
          <summary
            aria-label="アカウントメニュー"
            onClick={(event) => {
              if (accountMenu.current && !accountMenu.current.open) {
                accountMenu.current.dataset.pointerEntry = String(event.detail > 0);
              }
            }}
          >
            <Avatar avatar={avatar} />
            <span aria-hidden="true">···</span>
          </summary>
          <div className={styles.accountMenuPanel}>
            <p className={styles.accountMenuName}>{displayName}</p>
            <nav aria-label="アカウント操作">
              <Link to="/admin">サービス状態確認</Link>
              <Link to="/admin/prompts">プロンプト管理</Link>
            </nav>
            <ThemeSwitch theme={theme} onToggle={onThemeToggle} />
            <button
              className={commonStyles.secondaryButton}
              type="button"
              disabled={logoutPending}
              onClick={() => {
                if (accountMenu.current) {
                  accountMenu.current.open = false;
                  accountMenu.current.querySelector("summary")?.focus();
                }
                onLogout();
              }}
            >
              {logoutPending ? "ログアウト中…" : "ログアウト"}
            </button>
          </div>
        </details>
      </header>
      <aside className={styles.sidebar} aria-label="主要ナビゲーション">
        <Link className={styles.brandLink} to="/">
          <BrandMark compact />
          <ProductName />
        </Link>
        <nav className={styles.sidebarNavigation}>
          <div className={styles.primaryNavigation}>
            <NavLink
              className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
              to="/"
              end
            >
              <NavigationIcon kind="records" />
              議論の記録
            </NavLink>
            <NavLink
              className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
              to="/insights"
            >
              <NavigationIcon kind="insights" />
              いろいろな記録
            </NavLink>
            <NavLink
              className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
              to="/momotalk"
            >
              <NavigationIcon kind="momotalk" />
              モモトーク
            </NavLink>
            <NavLink
              className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
              to="/memorial"
            >
              <NavigationIcon kind="memorial" />
              メモリアルロビー
            </NavLink>
          </div>
          <section className={styles.systemAccess} aria-labelledby="system-access-label">
            <p className={styles.systemAccessLabel} id="system-access-label" lang="en">
              SYSTEM ACCESS
            </p>
            <NavLink
              aria-label="サービス状態確認"
              className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
              to="/admin"
              end
            >
              サービス状態確認
            </NavLink>
            <NavLink
              className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
              to="/admin/prompts"
            >
              プロンプト管理
            </NavLink>
          </section>
        </nav>
        <div className={styles.sidebarFooter}>
          <ThemeSwitch theme={theme} onToggle={onThemeToggle} />
          <div className={styles.account}>
            <Avatar avatar={avatar} />
            <span>{displayName}</span>
            <button
              className={commonStyles.quietButton}
              type="button"
              disabled={logoutPending}
              onClick={onLogout}
            >
              {logoutPending ? "ログアウト中…" : "ログアウト"}
            </button>
          </div>
        </div>
      </aside>
      <main className={styles.mainContent} id="main-content" tabIndex={-1}>
        {logoutError && (
          <div className={styles.logoutError} role="alert">
            <p>{logoutError}</p>
            <button
              className={commonStyles.secondaryButton}
              type="button"
              disabled={logoutPending}
              onClick={onLogout}
            >
              もう一度ログアウト
            </button>
          </div>
        )}
        {children}
      </main>
      <nav className={styles.mobileNav} aria-label="モバイルナビゲーション">
        <NavLink
          className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
          to="/"
          end
        >
          <NavigationIcon kind="records" />
          <span>記録</span>
        </NavLink>
        <NavLink
          className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
          to="/insights"
        >
          <NavigationIcon kind="insights" />
          <span>いろいろ</span>
        </NavLink>
        <NavLink
          aria-label="モモトーク"
          className={({ isActive }) =>
            `${isActive ? styles.navActive : styles.navLink} ${styles.mobileMemorialLink}`
          }
          to="/momotalk"
        >
          <NavigationIcon kind="momotalk" />
          <span>モモトーク</span>
        </NavLink>
        <NavLink
          aria-label="メモリアルロビー"
          className={({ isActive }) =>
            `${isActive ? styles.navActive : styles.navLink} ${styles.mobileMemorialLink}`
          }
          to="/memorial"
        >
          <NavigationIcon kind="memorial" />
          <span>メモリアル</span>
        </NavLink>
      </nav>
    </div>
  );
}
