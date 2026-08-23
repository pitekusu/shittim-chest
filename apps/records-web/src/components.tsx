import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type PropsWithChildren,
} from "react";
import { Link, NavLink } from "react-router-dom";

import type { AvatarRef, RecordListItem } from "./api";
import styles from "./App.module.css";
import type { Theme } from "./theme";

/* oxlint-disable jsx-a11y/prefer-tag-over-role -- Native select options cannot render avatars consistently. */

export function Avatar({ avatar }: { readonly avatar: AvatarRef }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  if (avatar.kind === "image" && avatar.url && avatar.url !== failedUrl) {
    return (
      <img
        className={styles.avatar}
        src={avatar.url}
        alt={avatar.alt}
        referrerPolicy="no-referrer"
        onError={() => setFailedUrl(avatar.url ?? null)}
      />
    );
  }
  return (
    <span className={`${styles.avatar} ${styles[`avatar-${avatar.fallbackVariant}`]}`}>
      <span aria-hidden="true" />
      <span className={styles.visuallyHidden}>{avatar.alt}</span>
    </span>
  );
}

export interface AvatarSelectOption<Value extends string> {
  readonly value: Value;
  readonly label: string;
  readonly avatar: AvatarRef | null;
}

export function AvatarSelect<Value extends string>({
  label,
  value,
  options,
  onChange,
}: {
  readonly label: string;
  readonly value: Value;
  readonly options: readonly AvatarSelectOption<Value>[];
  readonly onChange: (value: Value) => void;
}) {
  const id = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const [open, setOpen] = useState(false);
  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );
  const [activeIndex, setActiveIndex] = useState(selectedIndex);
  const selected = options[selectedIndex] ?? options[0];

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    optionRefs.current[activeIndex]?.focus();
  }, [activeIndex, open]);

  const openAt = (index: number) => {
    setActiveIndex(index);
    setOpen(true);
  };
  const closeAndFocusTrigger = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };
  const moveFocus = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let nextIndex: number | undefined;
    if (event.key === "ArrowDown") nextIndex = (index + 1) % options.length;
    if (event.key === "ArrowUp") nextIndex = (index - 1 + options.length) % options.length;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = options.length - 1;
    if (nextIndex !== undefined) {
      event.preventDefault();
      setActiveIndex(nextIndex);
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeAndFocusTrigger();
    }
    if (event.key === "Tab") setOpen(false);
  };

  return (
    <div className={styles.filterField} ref={rootRef}>
      <span id={`${id}-label`}>{label}</span>
      <div className={styles.avatarSelect}>
        <button
          ref={triggerRef}
          className={styles.avatarSelectButton}
          type="button"
          aria-label={label}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={`${id}-listbox`}
          aria-describedby={`${id}-value`}
          onClick={() => (open ? setOpen(false) : openAt(selectedIndex))}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              openAt(event.key === "ArrowDown" ? selectedIndex : options.length - 1);
            }
          }}
        >
          {selected?.avatar ? (
            <span className={styles.filterAvatar} aria-hidden="true">
              <Avatar avatar={selected.avatar} />
            </span>
          ) : (
            <span className={styles.filterAllIcon} aria-hidden="true">
              ◇
            </span>
          )}
          <span id={`${id}-value`}>{selected?.label ?? "すべて"}</span>
          <span className={styles.selectChevron} aria-hidden="true">
            ▾
          </span>
        </button>
        {open && (
          <div
            id={`${id}-listbox`}
            className={styles.avatarSelectMenu}
            role="listbox"
            aria-labelledby={`${id}-label`}
          >
            {options.map((option, index) => (
              <button
                key={option.value || "all"}
                ref={(node) => {
                  optionRefs.current[index] = node;
                }}
                className={styles.avatarSelectOption}
                type="button"
                role="option"
                aria-selected={option.value === value}
                tabIndex={index === activeIndex ? 0 : -1}
                onKeyDown={(event) => moveFocus(event, index)}
                onClick={() => {
                  onChange(option.value);
                  closeAndFocusTrigger();
                }}
              >
                {option.avatar ? (
                  <span className={styles.filterAvatar} aria-hidden="true">
                    <Avatar avatar={option.avatar} />
                  </span>
                ) : (
                  <span className={styles.filterAllIcon} aria-hidden="true">
                    ◇
                  </span>
                )}
                <span>{option.label}</span>
                {option.value === value && (
                  <span className={styles.selectedCheck} aria-hidden="true">
                    ✓
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

/* oxlint-enable jsx-a11y/prefer-tag-over-role */

export function BrandMark({ compact = false }: { readonly compact?: boolean }) {
  return (
    <span className={compact ? styles.brandMarkCompact : styles.brandMark} aria-hidden="true">
      <span />
    </span>
  );
}

function ProductNameLines() {
  return (
    <>
      <span className={styles.productNameLine}>THE SHITTIM</span>
      <span className={styles.productNameLine}>CHEST ARCHIVE</span>
    </>
  );
}

export function ProductName({ headingId }: { readonly headingId?: string }) {
  const accessibleName = "The Shittim Chest Archive";
  if (headingId) {
    return (
      <h1
        id={headingId}
        className={`${styles.productName} ${styles.japaneseHeading}`}
        aria-label={accessibleName}
        lang="en"
      >
        <ProductNameLines />
      </h1>
    );
  }
  return (
    <span className={styles.productName} aria-label={accessibleName} lang="en">
      <ProductNameLines />
    </span>
  );
}

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
      <div className={styles.backgroundGrid} aria-hidden="true" />
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
            <button className={styles.quietButton} type="button" lang="en" onClick={onLogout}>
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

const COMPLETED_DATE_TIME_FORMAT = new Intl.DateTimeFormat("ja-JP", {
  timeZone: "Asia/Tokyo",
  year: "numeric",
  month: "long",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

export function formatCompletedDateTime(value: string): string {
  return COMPLETED_DATE_TIME_FORMAT.format(new Date(value));
}

export function DebateCard({ record }: { readonly record: RecordListItem }) {
  const winner = record.participants.find(
    (participant) => participant.slot === record.result.winner,
  );
  return (
    <Link
      className={styles.debateCardLink}
      to={`/records/${record.recordId}`}
      aria-label={`「${record.questionPreview}」の記録を読む`}
    >
      <article className={styles.debateCard}>
        <div className={styles.cardMeta}>
          <time dateTime={record.completedAt}>{formatCompletedDateTime(record.completedAt)}</time>
          {record.result.tieBreakApplied && <span className={styles.tieBadge}>既定ルール決着</span>}
        </div>
        <h2 className={styles.japaneseText}>{record.questionPreview}</h2>
        <div className={styles.cardPeople}>
          <span className={styles.personSummary}>
            <Avatar avatar={record.requester.avatar} />
            <span>
              <small>依頼者</small>
              {record.requester.displayName}
            </span>
          </span>
          {winner && (
            <span className={styles.personSummary}>
              <Avatar avatar={winner.avatar} />
              <span>
                <small>勝者</small>
                {winner.displayName}
              </span>
            </span>
          )}
        </div>
        <span className={styles.cardAction} aria-hidden="true">
          記録を読む
        </span>
      </article>
    </Link>
  );
}

export function ErrorPanel({
  title,
  message,
  requestId,
  onRetry,
}: {
  readonly title: string;
  readonly message: string;
  readonly requestId?: string;
  readonly onRetry?: () => void;
}) {
  return (
    <section className={styles.messagePanel} role="alert">
      <span className={styles.errorRing} aria-hidden="true">
        !
      </span>
      <h1>{title}</h1>
      <p>{message}</p>
      {requestId && <p className={styles.requestId}>照会ID: {requestId}</p>}
      {onRetry && (
        <button className={styles.primaryButton} type="button" onClick={onRetry}>
          もう一度試す
        </button>
      )}
    </section>
  );
}
