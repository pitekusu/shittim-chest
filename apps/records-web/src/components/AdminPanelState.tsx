import adminStyles from "../styles/admin.module.css";
import commonStyles from "../styles/common.module.css";

export function AdminPanelState({
  busy = false,
  title,
  message,
  onRetry,
}: {
  readonly busy?: boolean;
  readonly title: string;
  readonly message: string;
  readonly onRetry?: () => void;
}): React.JSX.Element {
  return (
    <div
      className={adminStyles.panelState}
      aria-busy={busy || undefined}
      role={onRetry === undefined ? "status" : "alert"}
    >
      <div>
        <strong>{title}</strong>
        <span>{message}</span>
        {onRetry !== undefined && (
          <button
            className={commonStyles.secondaryButton}
            type="button"
            disabled={busy}
            onClick={onRetry}
          >
            {busy ? "再取得しています" : "もう一度試す"}
          </button>
        )}
      </div>
    </div>
  );
}
