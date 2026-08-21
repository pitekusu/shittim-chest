import { useState, type PropsWithChildren } from "react";
import { Link, NavLink } from "react-router-dom";

import type { AvatarRef, RecordListItem } from "./api";
import styles from "./App.module.css";

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

export function BrandMark({ compact = false }: { readonly compact?: boolean }) {
  return (
    <span className={compact ? styles.brandMarkCompact : styles.brandMark} aria-hidden="true">
      <span />
    </span>
  );
}

export function Layout({
  children,
  displayName,
  avatar,
  onLogout,
}: PropsWithChildren<{
  readonly displayName: string;
  readonly avatar: AvatarRef;
  readonly onLogout: () => void;
}>) {
  return (
    <div className={styles.appShell}>
      <div className={styles.backgroundGrid} aria-hidden="true" />
      <aside className={styles.sidebar} aria-label="主要ナビゲーション">
        <Link className={styles.brandLink} to="/">
          <BrandMark compact />
          <span>
            シッテムの箱
            <br />
            議事録
          </span>
        </Link>
        <nav>
          <NavLink
            className={({ isActive }) => (isActive ? styles.navActive : styles.navLink)}
            to="/"
            end
          >
            議論の記録
          </NavLink>
        </nav>
        <div className={styles.account}>
          <Avatar avatar={avatar} />
          <span>{displayName}</span>
          <button className={styles.quietButton} type="button" onClick={onLogout}>
            ログアウト
          </button>
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
        <button className={styles.mobileLogout} type="button" onClick={onLogout}>
          ログアウト
        </button>
      </nav>
    </div>
  );
}

function completedDate(value: string): string {
  return new Intl.DateTimeFormat("ja-JP", {
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(value));
}

export function DebateCard({ record }: { readonly record: RecordListItem }) {
  const winner = record.participants.find(
    (participant) => participant.slot === record.result.winner,
  );
  return (
    <article className={styles.debateCard}>
      <div className={styles.cardMeta}>
        <time dateTime={record.completedAt}>{completedDate(record.completedAt)}</time>
        {record.result.tieBreakApplied && <span className={styles.tieBadge}>既定ルール決着</span>}
      </div>
      <h2>
        <Link to={`/records/${record.recordId}`}>{record.questionPreview}</Link>
      </h2>
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
      <Link className={styles.cardLink} to={`/records/${record.recordId}`}>
        記録を読む
      </Link>
    </article>
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
