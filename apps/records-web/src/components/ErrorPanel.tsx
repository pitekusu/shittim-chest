import styles from "../styles/common.module.css";

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
      <h1 tabIndex={-1}>{title}</h1>
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
