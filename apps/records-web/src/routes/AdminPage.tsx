import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { getAdminStatus, refreshAdminStatus } from "../api/admin";
import { RecordsApiError } from "../api/http";
import type { AdminHealthState, AdminService, AdminStatusResponse } from "../api/types";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import { formatCompletedDateTime } from "../lib/dateTime";
import adminStyles from "../styles/admin.module.css";
import commonStyles from "../styles/common.module.css";
import routeStyles from "../styles/routeMotion.module.css";

const SERVICE_LABELS: Readonly<Record<AdminService, string>> = {
  ecs: "ECS",
  ecr: "ECR",
  inspector: "INSPECTOR",
  s3: "S3",
  dynamodb: "DYNAMODB",
  lambda: "LAMBDA",
  cloudfront: "CLOUDFRONT",
  sqs: "SQS",
};
const HEALTH_LABELS: Readonly<Record<AdminHealthState, string>> = {
  healthy: "正常",
  warning: "注意",
  critical: "異常",
  unknown: "未確認",
};

interface AdminPageProps {
  readonly isAdmin: boolean;
  readonly csrfToken: string;
}

function newIdempotencyKey(): string {
  return crypto.randomUUID();
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof RecordsApiError ? error.message : fallback;
}

function healthTone(state: AdminHealthState): "normal" | "warning" | "critical" | "unknown" {
  if (state === "healthy") return "normal";
  if (state === "critical") return "critical";
  if (state === "unknown") return "unknown";
  return "warning";
}

function formatMetricValue(value: string | number | boolean | null): string {
  if (value === null) return "—";
  if (typeof value === "boolean") return value ? "有効" : "無効";
  return String(value);
}

function AdminAccessDenied(): React.JSX.Element {
  return (
    <section
      className={`${adminStyles.accessDenied} ${routeStyles.routeMotionItem}`}
      data-route-motion-ready=""
      data-route-motion-terminal=""
    >
      <div className={adminStyles.accessDeniedCopy}>
        <span className={adminStyles.deniedMark} aria-hidden="true" />
        <p className={adminStyles.deniedCode} lang="en">
          403
        </p>
        <h1 lang="en" tabIndex={-1}>
          ACCESS DENIED
        </h1>
        <p className={commonStyles.japaneseText}>この画面を利用する権限がありません。</p>
        <Link className={commonStyles.primaryButton} to="/">
          記録一覧へ戻る
        </Link>
      </div>
    </section>
  );
}

function PanelState({
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
          <button className={commonStyles.secondaryButton} type="button" onClick={onRetry}>
            もう一度試す
          </button>
        )}
      </div>
    </div>
  );
}

function OverviewPanel({
  status,
}: {
  readonly status: AdminStatusResponse | undefined;
}): React.JSX.Element {
  const state = status?.overall.state ?? "unknown";
  return (
    <section
      className={adminStyles.adminPanel}
      id="admin-overview"
      aria-labelledby="overview-title"
    >
      <header className={adminStyles.panelHeader}>
        <div>
          <p className={adminStyles.panelEyebrow} lang="en">
            OVERVIEW
          </p>
          <h2 id="overview-title">現在の状態</h2>
          <p>AWS全体の概要と取得状態を確認できます。</p>
        </div>
      </header>
      <dl className={adminStyles.overviewGrid}>
        <div className={adminStyles.overviewItem}>
          <dt>アクセス</dt>
          <dd>管理者</dd>
        </div>
        <div className={adminStyles.overviewItem}>
          <dt>AWS全体</dt>
          <dd>
            <span className={adminStyles.stateBadge} data-tone={healthTone(state)}>
              {HEALTH_LABELS[state]}
            </span>
          </dd>
        </div>
        <div className={adminStyles.overviewItem}>
          <dt>Critical alarm</dt>
          <dd>{status?.overall.criticalAlarms ?? "—"}</dd>
        </div>
        <div className={adminStyles.overviewItem}>
          <dt>状態取得</dt>
          <dd>{status?.stale ? "再取得待ち" : status ? "最新" : "未確認"}</dd>
        </div>
      </dl>
    </section>
  );
}

function AwsStatusPanel({
  csrfToken,
  status,
}: {
  readonly csrfToken: string;
  readonly status: UseQueryResult<AdminStatusResponse>;
}): React.JSX.Element {
  const client = useQueryClient();
  const refresh = useMutation({
    mutationFn: () => refreshAdminStatus(csrfToken, newIdempotencyKey()),
    onSuccess: (response) => client.setQueryData(["admin", "status"], response),
  });
  useAuthenticationRecovery(refresh.error);
  const data = status.data;

  return (
    <section
      className={adminStyles.adminPanel}
      id="admin-status"
      aria-labelledby="status-title"
      data-route-motion-terminal=""
    >
      <header className={adminStyles.panelHeader}>
        <div>
          <p className={adminStyles.panelEyebrow} lang="en">
            AWS STATUS
          </p>
          <h2 id="status-title">サービス状態</h2>
          <p>閲覧専用の情報です。この画面から運用操作は行えません。</p>
        </div>
        <button
          className={`${commonStyles.secondaryButton} ${adminStyles.refreshButton}`}
          type="button"
          disabled={refresh.isPending}
          onClick={() => refresh.mutate()}
        >
          {refresh.isPending ? "更新しています" : "状態を更新"}
        </button>
      </header>
      {status.isPending && !data && (
        <PanelState
          busy
          title="AWSの状態を読み込んでいます"
          message="最大60秒のcacheを利用します。"
        />
      )}
      {status.isError && !data && (
        <PanelState
          title="AWSの状態を読み込めませんでした"
          message={errorMessage(status.error, "通信状態を確認してください。")}
          onRetry={() => void status.refetch()}
        />
      )}
      {refresh.isError && (
        <PanelState
          title="状態を更新できませんでした"
          message={errorMessage(refresh.error, "直前に取得した状態を表示しています。")}
        />
      )}
      {data && (
        <>
          {(data.stale || data.overall.partial) && (
            <output className={adminStyles.statusNotice}>
              {data.stale
                ? "60秒cacheの期限を過ぎた状態です。更新後も取得できない項目は未確認として残します。"
                : "一部のサービスを確認できませんでした。取得できた状態だけを表示しています。"}
            </output>
          )}
          <dl className={adminStyles.overviewGrid}>
            <div className={adminStyles.overviewItem}>
              <dt>全体状態</dt>
              <dd>
                <span className={adminStyles.stateBadge} data-tone={healthTone(data.overall.state)}>
                  {HEALTH_LABELS[data.overall.state]}
                </span>
              </dd>
            </div>
            <div className={adminStyles.overviewItem}>
              <dt>Warning / Critical</dt>
              <dd>
                {data.overall.warningAlarms} / {data.overall.criticalAlarms}
              </dd>
            </div>
            <div className={adminStyles.overviewItem}>
              <dt>取得日時</dt>
              <dd>{formatCompletedDateTime(data.generatedAt)}</dd>
            </div>
          </dl>
          <div className={adminStyles.statusGrid}>
            {data.sections.map((section) => (
              <article className={adminStyles.statusCard} key={section.service}>
                <header className={adminStyles.statusCardHeader}>
                  <h3>{SERVICE_LABELS[section.service]}</h3>
                  <span className={adminStyles.stateBadge} data-tone={healthTone(section.state)}>
                    {HEALTH_LABELS[section.state]}
                  </span>
                </header>
                <p className={commonStyles.japaneseText}>{section.summary}</p>
                <dl className={adminStyles.statusFacts}>
                  {section.metrics.map((metric) => (
                    <div className={adminStyles.statusFact} key={metric.name}>
                      <dt>{metric.name}</dt>
                      <dd>{formatMetricValue(metric.value)}</dd>
                    </div>
                  ))}
                </dl>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function AuthorizedAdminPage({ csrfToken }: { readonly csrfToken: string }): React.JSX.Element {
  const status = useQuery({ queryKey: ["admin", "status"], queryFn: getAdminStatus });
  useAuthenticationRecovery(status.error);

  return (
    <div className={adminStyles.adminPage} data-route-motion-ready="">
      <header className={`${adminStyles.adminHeader} ${routeStyles.routeMotionItem}`}>
        <p className={commonStyles.eyebrow} lang="en">
          SHITTIM CHEST CONTROL
        </p>
        <h1 lang="en" tabIndex={-1}>
          ADMIN
        </h1>
        <p className={commonStyles.japaneseText}>AWSの稼働状態を、安全な境界の内側で確認します。</p>
      </header>
      <div className={adminStyles.adminWorkspace}>
        <nav className={adminStyles.sectionNavigation} aria-label="ADMIN画面内ナビゲーション">
          <a href="#admin-overview">現在の状態</a>
          <a href="#admin-status">AWS状態</a>
        </nav>
        <div className={adminStyles.adminContent}>
          <OverviewPanel status={status.data} />
          <AwsStatusPanel csrfToken={csrfToken} status={status} />
        </div>
      </div>
    </div>
  );
}

export default function AdminPage({ isAdmin, csrfToken }: AdminPageProps): React.JSX.Element {
  return isAdmin ? <AuthorizedAdminPage csrfToken={csrfToken} /> : <AdminAccessDenied />;
}
