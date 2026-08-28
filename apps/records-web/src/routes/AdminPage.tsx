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

type AdminStatusSection = AdminStatusResponse["sections"][number];
type AdminStatusMetric = AdminStatusSection["metrics"][number];

const SERVICE_PRESENTATION: Readonly<
  Record<AdminService, { readonly name: string; readonly purpose: string }>
> = {
  ecs: { name: "ECS", purpose: "コンテナ実行基盤" },
  ecr: { name: "ECR", purpose: "コンテナイメージ" },
  inspector: { name: "Inspector", purpose: "脆弱性検査" },
  s3: { name: "S3", purpose: "オブジェクト保管" },
  dynamodb: { name: "DynamoDB", purpose: "データストア" },
  lambda: { name: "Lambda", purpose: "サーバーレス処理" },
  cloudfront: { name: "CloudFront", purpose: "Web配信" },
  sqs: { name: "SQS", purpose: "失敗イベント保管" },
  apigateway: { name: "API Gateway", purpose: "API入口" },
  eventbridge: { name: "EventBridge", purpose: "定期・イベント配信" },
  cloudformation: { name: "CloudFormation", purpose: "Stack管理" },
  sns: { name: "SNS", purpose: "運用通知" },
  ssm: { name: "Parameter Store", purpose: "設定準備" },
  cost_governance: { name: "Cost Management", purpose: "予算・異常検知" },
  signer: { name: "Signer", purpose: "コンテナ署名" },
  external: { name: "External APIs", purpose: "外部集計" },
};

const HEALTH_LABELS: Readonly<Record<AdminHealthState, string>> = {
  healthy: "正常",
  warning: "注意",
  critical: "異常",
  unknown: "未確認",
};

const SIMPLE_METRICS: Readonly<
  Partial<Record<AdminService, readonly { readonly name: string; readonly label: string }[]>>
> = {
  inspector: [
    { name: "active_critical", label: "重大" },
    { name: "active_high", label: "高" },
    { name: "active_medium", label: "中" },
    { name: "active_low", label: "低" },
    { name: "active_untriaged", label: "未分類" },
    { name: "coverage_active", label: "検査対象" },
    { name: "last_scanned_at", label: "最終検査日時" },
  ],
  cloudfront: [
    { name: "enabled", label: "配信" },
    { name: "deployment_status", label: "デプロイ状態" },
    { name: "tls_policy", label: "TLSポリシー" },
    { name: "certificate_key_algorithm", label: "証明書鍵" },
    { name: "certificate_expires_at", label: "証明書期限" },
    { name: "hour_requests", label: "直近1時間のリクエスト" },
    { name: "hour_4xx_rate", label: "4xx率" },
    { name: "hour_5xx_rate", label: "5xx率" },
  ],
  sqs: [
    { name: "visible_messages", label: "未処理メッセージ" },
    { name: "inflight_messages", label: "処理中メッセージ" },
    { name: "delayed_messages", label: "遅延メッセージ" },
    { name: "oldest_message_age_seconds", label: "最古メッセージ" },
    { name: "encrypted", label: "暗号化" },
    { name: "retention_seconds", label: "保存期間" },
  ],
  sns: [
    { name: "confirmed_subscriptions", label: "確認済み購読" },
    { name: "pending_subscriptions", label: "確認待ち購読" },
    { name: "day_delivered", label: "24時間の配信" },
    { name: "day_failed", label: "24時間の失敗" },
  ],
  signer: [
    { name: "status", label: "署名profile" },
    { name: "platform", label: "署名方式" },
    { name: "validity_value", label: "署名有効期間" },
    { name: "validity_unit", label: "期間単位" },
  ],
};

const S3_RESOURCES = [
  { key: "web", label: "Webサイト" },
  { key: "media", label: "画像" },
  { key: "release", label: "リリース成果物" },
] as const;

const DYNAMODB_RESOURCES = [
  { key: "debate", label: "議論" },
  { key: "archive", label: "議事録" },
  { key: "statistics", label: "統計" },
  { key: "session", label: "セッション" },
] as const;

const LAMBDA_RESOURCES = [
  { key: "discord_ingress", label: "Discord受付" },
  { key: "discord_status", label: "Discord状態通知" },
  { key: "image_admission", label: "イメージ審査" },
  { key: "runtime_reconciler", label: "Runtime調整" },
  { key: "records_projector", label: "記録投影" },
  { key: "records_backfill", label: "過去記録取込" },
  { key: "records_auth", label: "認証API" },
  { key: "records_read", label: "閲覧API" },
  { key: "records_ranking", label: "ランキング集計" },
  { key: "records_cost", label: "費用集計" },
  { key: "records_admin_status", label: "管理状態API" },
] as const;

const API_RESOURCES = [
  { key: "discord", label: "Discord受付" },
  { key: "records", label: "議事録API" },
] as const;

const EVENT_RESOURCES = [
  { key: "runtime", label: "Runtime調整", hasDeliveryMetrics: false },
  { key: "ranking", label: "ランキング集計", hasDeliveryMetrics: true },
  { key: "aws_fx", label: "AWS・為替集計", hasDeliveryMetrics: true },
  { key: "openai", label: "OpenAI集計", hasDeliveryMetrics: true },
  { key: "abnormal_stop", label: "異常終了通知", hasDeliveryMetrics: true },
] as const;

const STACK_RESOURCES = [
  { key: "stateful", label: "基盤データ" },
  { key: "release_identity", label: "リリース認証" },
  { key: "runtime", label: "討論Runtime" },
  { key: "operations", label: "運用監視" },
  { key: "cost_governance", label: "費用管理" },
  { key: "records_stateful", label: "議事録データ" },
  { key: "records_application", label: "議事録Application" },
  { key: "records_edge", label: "議事録Edge" },
] as const;

const CONFIGURATION_GROUPS = [
  { key: "discord", label: "Discord連携" },
  { key: "runtime", label: "討論Runtime" },
  { key: "records", label: "議事録システム" },
  { key: "cost", label: "費用集計" },
] as const;

const EXTERNAL_SOURCES = [
  { key: "aws", label: "AWS Cost Explorer" },
  { key: "openai", label: "OpenAI Costs" },
  { key: "frankfurter", label: "Frankfurter為替" },
] as const;

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

function formatMetricValue(name: string, value: AdminStatusMetric["value"]): string {
  if (value === null) return "未取得";
  if (typeof value === "boolean") {
    if (name.endsWith("_image_present")) return value ? "登録済み" : "未登録";
    if (name.endsWith("_initial_complete")) return value ? "完了" : "準備中";
    if (name.endsWith("_fresh")) return value ? "最新" : "要確認";
    if (name === "runtime_prompt_pointer_present" || name === "anomaly_subscription") {
      return value ? "登録済み" : "未登録";
    }
    return value ? "有効" : "無効";
  }
  if (typeof value === "number") {
    if (name.endsWith("_size_bytes")) return formatBytes(value);
    if (name.endsWith("_percent")) return `${value.toLocaleString("ja-JP")}%`;
    if (name === "task_definition_revision") return `rev. ${value.toLocaleString("ja-JP")}`;
    if (name.endsWith("_seconds")) {
      if (name === "retention_seconds" && value % 86400 === 0) return `${value / 86400}日`;
      return `${value}秒`;
    }
    return value.toLocaleString("ja-JP");
  }
  if (name === "service_status" && value === "ACTIVE") return "有効";
  if (name.endsWith("_at") && /^\d{4}-\d{2}-\d{2}T/.test(value)) {
    return formatCompletedDateTime(value);
  }
  if (name.endsWith("_seconds") && /^\d+(?:\.\d+)?$/.test(value)) {
    const seconds = Number(value);
    if (name === "retention_seconds" && seconds % 86400 === 0) return `${seconds / 86400}日`;
    return `${seconds.toLocaleString("ja-JP")}秒`;
  }
  if (name.endsWith("_percent") && /^\d+(?:\.\d+)?$/.test(value)) return `${value}%`;
  if (name.endsWith("_rate") && /^\d+(?:\.\d+)?$/.test(value)) return `${value}%`;
  if ((name.endsWith("_duration") || name.endsWith("_latency")) && /^\d+(?:\.\d+)?$/.test(value)) {
    return `${value} ms`;
  }
  const translated: Readonly<Record<string, string>> = {
    ACTIVE: "稼働中",
    Active: "稼働中",
    AES256: "AES-256",
    COMPLETED: "完了",
    CUSTOM_CAPACITY_PROVIDER: "カスタムCapacity Provider",
    Completed: "完了",
    DOCKER_LIST: "Docker manifest list",
    DOCKER_V2: "Docker image",
    DISABLED: "無効",
    Deployed: "配信済み",
    Disabled: "無効",
    ENABLED: "有効",
    ECS: "ECSローリング更新",
    Enabled: "有効",
    FAILED: "失敗",
    FARGATE: "Fargate On-Demand",
    FARGATE_MIXED: "Fargate混在",
    FARGATE_SPOT: "Fargate Spot",
    HEALTHY: "正常",
    IMMUTABLE: "変更不可",
    IN_PROGRESS: "反映中",
    IN_SYNC: "一致",
    ISSUED: "発行済み",
    KMS: "KMS",
    KMS_DSSE: "KMS二層暗号化",
    MONTHS: "か月",
    MUTABLE: "変更可能",
    NOT_CHECKED: "未検査",
    OCI_IMAGE: "OCI image",
    OCI_INDEX: "OCI image index",
    OTHER: "その他",
    REPLICA: "タスク数制御",
    SIGNATURE: "署名artifact",
    Successful: "正常",
    UPDATE_COMPLETE: "更新完了",
    CREATE_COMPLETE: "作成完了",
    UNHEALTHY: "注意",
    UNKNOWN: "未確認",
    event_pattern: "イベント受信",
    "event pattern": "イベント受信",
    unknown: "未確認",
  };
  return translated[value] ?? value;
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "未取得";
  const units = ["B", "KiB", "MiB", "GiB"] as const;
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toLocaleString("ja-JP", { maximumFractionDigits: digits })} ${units[unitIndex]}`;
}

function metricLookup(
  metrics: readonly AdminStatusMetric[],
): ReadonlyMap<string, AdminStatusMetric> {
  return new Map(metrics.map((metric) => [metric.name, metric]));
}

function metricValue(metrics: ReadonlyMap<string, AdminStatusMetric>, name: string): string {
  const metric = metrics.get(name);
  return formatMetricValue(name, metric?.value ?? null);
}

function ServiceIcon({ service }: { readonly service: AdminService }): React.JSX.Element {
  const common = {
    className: adminStyles.serviceIconSvg,
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.7,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
    "aria-hidden": true,
  };
  if (service === "ecs") {
    return (
      <svg {...common}>
        <path d="m12 3 7.5 4.2v8.6L12 20l-7.5-4.2V7.2L12 3Z" />
        <path d="m4.8 7.4 7.2 4.1 7.2-4.1M12 11.5V20" />
      </svg>
    );
  }
  if (service === "ecr") {
    return (
      <svg {...common}>
        <path d="M5 6.5h14v11H5zM8 3.5h8v3H8z" />
        <path d="M8 10h8M8 14h5" />
      </svg>
    );
  }
  if (service === "inspector") {
    return (
      <svg {...common}>
        <path d="M12 3.2 19 6v5.1c0 4.4-2.8 7.7-7 9.7-4.2-2-7-5.3-7-9.7V6l7-2.8Z" />
        <path d="m8.8 12 2 2 4.5-4.5" />
      </svg>
    );
  }
  if (service === "s3") {
    return (
      <svg {...common}>
        <ellipse cx="12" cy="5.5" rx="6.5" ry="2.5" />
        <path d="M5.5 5.5v6c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-6M5.5 11.5v6c0 1.4 2.9 2.5 6.5 2.5s6.5-1.1 6.5-2.5v-6" />
      </svg>
    );
  }
  if (service === "dynamodb") {
    return (
      <svg {...common}>
        <path d="M4 4h16v16H4zM4 9.3h16M4 14.7h16M9.3 4v16" />
      </svg>
    );
  }
  if (service === "lambda") {
    return (
      <svg {...common}>
        <path d="M7 4h4l7 16h-4l-2.2-5H8.5L6 20H2l7-14" />
      </svg>
    );
  }
  if (service === "cloudfront") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M3.8 12h16.4M12 3.5c2.3 2.4 3.5 5.2 3.5 8.5S14.3 18.1 12 20.5C9.7 18.1 8.5 15.3 8.5 12S9.7 5.9 12 3.5Z" />
      </svg>
    );
  }
  if (service === "apigateway") {
    return (
      <svg {...common}>
        <path d="M5 5h5v5H5zM14 14h5v5h-5z" />
        <path d="M10 7.5h4a3 3 0 0 1 3 3V14M14 16.5h-4a3 3 0 0 1-3-3V10" />
      </svg>
    );
  }
  if (service === "eventbridge") {
    return (
      <svg {...common}>
        <circle cx="6" cy="12" r="2.2" />
        <circle cx="18" cy="6" r="2.2" />
        <circle cx="18" cy="18" r="2.2" />
        <path d="m8 11 7.8-4M8 13l7.8 4" />
      </svg>
    );
  }
  if (service === "cloudformation") {
    return (
      <svg {...common}>
        <path d="m12 3 7 4v10l-7 4-7-4V7l7-4Z" />
        <path d="m5 7 7 4 7-4M12 11v10M8.5 5l7 4" />
      </svg>
    );
  }
  if (service === "sns") {
    return (
      <svg {...common}>
        <path d="M7 9a5 5 0 0 1 10 0c0 5 2 5 2 7H5c0-2 2-2 2-7Z" />
        <path d="M10 19h4M3 7c0-2 1-3 2-4M21 7c0-2-1-3-2-4" />
      </svg>
    );
  }
  if (service === "ssm") {
    return (
      <svg {...common}>
        <path d="M4 5h16v14H4zM8 9h8M8 13h5" />
        <circle cx="17" cy="15.5" r="1.5" />
      </svg>
    );
  }
  if (service === "cost_governance") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M15.5 8.5c-.7-.8-1.8-1.2-3.1-1.2-1.8 0-3.2.9-3.2 2.2 0 3.5 6.2 1.2 6.2 4.8 0 1.4-1.4 2.4-3.4 2.4-1.5 0-2.8-.5-3.6-1.4M12 5.5v13" />
      </svg>
    );
  }
  if (service === "signer") {
    return (
      <svg {...common}>
        <path d="M12 3.5 18.5 6v5c0 4.2-2.5 7.3-6.5 9.5-4-2.2-6.5-5.3-6.5-9.5V6L12 3.5Z" />
        <path d="m8.7 12.2 2.1 2.1 4.6-4.6" />
      </svg>
    );
  }
  if (service === "external") {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="8.5" />
        <path d="M3.5 12h17M12 3.5c2.1 2.3 3.2 5.1 3.2 8.5S14.1 18.2 12 20.5M16 8h4V4" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <path d="M4 7h11M4 12h16M9 17h11" />
      <path d="m12 4 3 3-3 3M12 14l-3 3 3 3" />
    </svg>
  );
}

function SystemSignalIcon(): React.JSX.Element {
  return (
    <svg
      className={adminStyles.systemSignalSvg}
      viewBox="0 0 64 64"
      fill="none"
      stroke="currentColor"
      aria-hidden="true"
    >
      <circle cx="32" cy="32" r="23" strokeWidth="1.4" />
      <circle cx="32" cy="32" r="15" strokeWidth="1" opacity=".5" />
      <path d="M11 32h11l4-8 8 17 5-10h14" strokeWidth="2.2" strokeLinecap="round" />
      <path d="m50 12 2 4 4 2-4 2-2 4-2-4-4-2 4-2 2-4Z" fill="currentColor" stroke="none" />
    </svg>
  );
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

function StateBadge({ state }: { readonly state: AdminHealthState }): React.JSX.Element {
  return (
    <span className={adminStyles.stateBadge} data-tone={healthTone(state)}>
      <span className={adminStyles.stateDot} aria-hidden="true" />
      {HEALTH_LABELS[state]}
    </span>
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
      className={`${adminStyles.adminPanel} ${adminStyles.overviewPanel}`}
      id="admin-overview"
      aria-labelledby="overview-title"
    >
      <header className={adminStyles.panelHeader}>
        <div>
          <p className={adminStyles.panelEyebrow} lang="en">
            OVERVIEW
          </p>
          <h2 id="overview-title">現在の状態</h2>
        </div>
      </header>
      <div className={adminStyles.overviewBody}>
        <div className={adminStyles.systemSignal} data-tone={healthTone(state)}>
          <span className={adminStyles.systemSignalIcon}>
            <SystemSignalIcon />
          </span>
          <div>
            <span className={adminStyles.systemSignalLabel}>システム全体</span>
            <StateBadge state={state} />
          </div>
        </div>
        <dl className={adminStyles.overviewStats}>
          <div className={adminStyles.overviewStat} data-tone="warning">
            <dt>
              <span className={adminStyles.overviewStatIcon} aria-hidden="true">
                !
              </span>
              警告アラーム
            </dt>
            <dd>{status?.overall.warningAlarms ?? "—"}</dd>
          </div>
          <div className={adminStyles.overviewStat} data-tone="critical">
            <dt>
              <span className={adminStyles.overviewStatIcon} aria-hidden="true">
                ×
              </span>
              重大アラーム
            </dt>
            <dd>{status?.overall.criticalAlarms ?? "—"}</dd>
          </div>
          <div className={adminStyles.overviewStat} data-tone="time">
            <dt>
              <span className={adminStyles.overviewStatIcon} aria-hidden="true">
                ◷
              </span>
              状態取得日時
            </dt>
            <dd>
              {status ? (
                <time dateTime={status.generatedAt}>
                  {formatCompletedDateTime(status.generatedAt)}
                </time>
              ) : (
                "未確認"
              )}
            </dd>
          </div>
        </dl>
      </div>
    </section>
  );
}

function SimpleMetricGrid({
  section,
}: {
  readonly section: AdminStatusSection;
}): React.JSX.Element {
  const metrics = metricLookup(section.metrics);
  const definitions = SIMPLE_METRICS[section.service] ?? [];
  return (
    <dl className={adminStyles.metricGrid}>
      {definitions.map((definition) => (
        <div className={adminStyles.metricItem} key={definition.name}>
          <span className={adminStyles.metricMarker} aria-hidden="true" />
          <dt>{definition.label}</dt>
          <dd>{metricValue(metrics, definition.name)}</dd>
        </div>
      ))}
    </dl>
  );
}

/* oxlint-disable jsx-a11y/no-noninteractive-tabindex -- Horizontally scrollable data tables need keyboard focus. */
function EcsMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <>
      <dl className={adminStyles.readinessGrid}>
        <div>
          <dt>タスク稼働</dt>
          <dd>
            <strong>{metricValue(metrics, "running_count")}</strong>
            <span>／ {metricValue(metrics, "desired_count")} 希望</span>
          </dd>
        </div>
        <div>
          <dt>起動待ち</dt>
          <dd>
            <strong>{metricValue(metrics, "pending_count")}</strong>
            <span> タスク</span>
          </dd>
        </div>
        <div>
          <dt>デプロイ</dt>
          <dd>
            <strong>{metricValue(metrics, "rollout_state")}</strong>
            <span>／失敗 {metricValue(metrics, "failed_task_count")}</span>
          </dd>
        </div>
        <div>
          <dt>進行中の議論</dt>
          <dd>
            <strong>{metricValue(metrics, "active_debates")}</strong>
            <span> 件</span>
          </dd>
        </div>
      </dl>
      <section
        className={adminStyles.tableScroller}
        aria-label="ECS構成とデプロイ状態"
        tabIndex={0}
      >
        <table className={`${adminStyles.resourceTable} ${adminStyles.wideTable}`}>
          <thead>
            <tr>
              <th scope="col">状態</th>
              <th scope="col">実行基盤</th>
              <th scope="col">基盤バージョン</th>
              <th scope="col">タスク定義</th>
              <th scope="col">制御方式</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>{metricValue(metrics, "service_status")}</td>
              <td>{metricValue(metrics, "launch_mode")}</td>
              <td>{metricValue(metrics, "platform_version")}</td>
              <td>{metricValue(metrics, "task_definition_revision")}</td>
              <td>{metricValue(metrics, "scheduling_strategy")}</td>
            </tr>
          </tbody>
        </table>
      </section>
      <dl className={adminStyles.inlineFacts}>
        <div>
          <dt>更新方式</dt>
          <dd>{metricValue(metrics, "deployment_controller")}</dd>
        </div>
        <div>
          <dt>デプロイ数</dt>
          <dd>{metricValue(metrics, "deployment_count")}</dd>
        </div>
        <div>
          <dt>正常稼働率</dt>
          <dd>
            {metricValue(metrics, "minimum_healthy_percent")}～
            {metricValue(metrics, "maximum_percent")}
          </dd>
        </div>
        <div>
          <dt>デプロイ更新</dt>
          <dd>{metricValue(metrics, "deployment_updated_at")}</dd>
        </div>
        <div>
          <dt>デプロイ障害検知</dt>
          <dd>{metricValue(metrics, "circuit_breaker_enabled")}</dd>
        </div>
        <div>
          <dt>自動rollback</dt>
          <dd>{metricValue(metrics, "circuit_breaker_rollback")}</dd>
        </div>
        <div>
          <dt>ECS Exec</dt>
          <dd>{metricValue(metrics, "execute_command_enabled")}</dd>
        </div>
        <div>
          <dt>配送待ち</dt>
          <dd>{metricValue(metrics, "outbox_pending")}</dd>
        </div>
        <div>
          <dt>Runtime Prompt版</dt>
          <dd>{metricValue(metrics, "runtime_prompt_revision")}</dd>
        </div>
        <div>
          <dt>最終応答から</dt>
          <dd>{metricValue(metrics, "heartbeat_age_seconds")}</dd>
        </div>
      </dl>
    </>
  );
}

function EcrMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <>
      <dl className={adminStyles.readinessGrid}>
        <div>
          <dt>格納イメージ等</dt>
          <dd>
            <strong>{metricValue(metrics, "repository_image_count")}</strong>
            <span> 件</span>
          </dd>
        </div>
        <div>
          <dt>タグ付き</dt>
          <dd>
            <strong>{metricValue(metrics, "repository_tagged_image_count")}</strong>
            <span> 件</span>
          </dd>
        </div>
        <div>
          <dt>タグなし</dt>
          <dd>
            <strong>{metricValue(metrics, "repository_untagged_image_count")}</strong>
            <span> 件</span>
          </dd>
        </div>
        <div>
          <dt>合計容量（概算）</dt>
          <dd>
            <strong>{metricValue(metrics, "repository_total_size_bytes")}</strong>
          </dd>
        </div>
      </dl>
      <section className={adminStyles.tableScroller} aria-label="承認済みECRイメージ" tabIndex={0}>
        <table className={`${adminStyles.resourceTable} ${adminStyles.ecrTable}`}>
          <thead>
            <tr>
              <th scope="col">用途</th>
              <th scope="col">登録</th>
              <th scope="col">形式</th>
              <th scope="col">容量</th>
              <th scope="col">タグ数</th>
              <th scope="col">登録日時</th>
              <th scope="col">最終取得記録</th>
            </tr>
          </thead>
          <tbody>
            {[
              { key: "normal", label: "通常版" },
              { key: "break_glass", label: "緊急版" },
            ].map((image) => (
              <tr key={image.key}>
                <th scope="row">{image.label}</th>
                <td>{metricValue(metrics, `${image.key}_image_present`)}</td>
                <td>{metricValue(metrics, `${image.key}_media_type`)}</td>
                <td>{metricValue(metrics, `${image.key}_size_bytes`)}</td>
                <td>{metricValue(metrics, `${image.key}_tag_count`)}</td>
                <td>{metricValue(metrics, `${image.key}_pushed_at`)}</td>
                <td>{metricValue(metrics, `${image.key}_last_pulled_at`)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <dl className={adminStyles.inlineFacts}>
        <div>
          <dt>タグ変更</dt>
          <dd>{metricValue(metrics, "tag_mutability")}</dd>
        </div>
        <div>
          <dt>暗号化</dt>
          <dd>{metricValue(metrics, "encryption_type")}</dd>
        </div>
        <div>
          <dt>登録時基本スキャン</dt>
          <dd>{metricValue(metrics, "scan_on_push")}</dd>
        </div>
        <div>
          <dt>保管庫の作成日時</dt>
          <dd>{metricValue(metrics, "repository_created_at")}</dd>
        </div>
        <div>
          <dt>最新登録</dt>
          <dd>{metricValue(metrics, "repository_latest_pushed_at")}</dd>
        </div>
      </dl>
    </>
  );
}
/* oxlint-enable jsx-a11y/no-noninteractive-tabindex */

function S3Metrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- A horizontally scrollable data table needs keyboard focus.
    <section className={adminStyles.tableScroller} aria-label="S3保護設定" tabIndex={0}>
      <table className={adminStyles.resourceTable}>
        <thead>
          <tr>
            <th scope="col">保存先</th>
            <th scope="col">バージョン管理</th>
            <th scope="col">暗号化</th>
            <th scope="col">公開アクセス遮断</th>
          </tr>
        </thead>
        <tbody>
          {S3_RESOURCES.map((resource) => (
            <tr key={resource.key}>
              <th scope="row">{resource.label}</th>
              <td>{metricValue(metrics, `${resource.key}_versioning`)}</td>
              <td>{metricValue(metrics, `${resource.key}_encrypted`)}</td>
              <td>{metricValue(metrics, `${resource.key}_public_access_blocked`)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function DynamoDbMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <>
      {/* oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- A horizontally scrollable data table needs keyboard focus. */}
      <section className={adminStyles.tableScroller} aria-label="DynamoDBテーブル状態" tabIndex={0}>
        <table className={`${adminStyles.resourceTable} ${adminStyles.wideTable}`}>
          <thead>
            <tr>
              <th scope="col">テーブル</th>
              <th scope="col">状態</th>
              <th scope="col">時点復旧</th>
              <th scope="col">削除保護</th>
              <th scope="col">有効期限</th>
              <th scope="col">項目数</th>
              <th scope="col">スロットル（読／書）</th>
            </tr>
          </thead>
          <tbody>
            {DYNAMODB_RESOURCES.map((resource) => (
              <tr key={resource.key}>
                <th scope="row">{resource.label}</th>
                <td>{metricValue(metrics, `${resource.key}_status`)}</td>
                <td>{metricValue(metrics, `${resource.key}_pitr`)}</td>
                <td>{metricValue(metrics, `${resource.key}_deletion_protection`)}</td>
                <td>{metricValue(metrics, `${resource.key}_ttl`)}</td>
                <td>{metricValue(metrics, `${resource.key}_item_count`)}</td>
                <td>
                  {metricValue(metrics, `${resource.key}_read_throttles`)}／
                  {metricValue(metrics, `${resource.key}_write_throttles`)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <dl className={adminStyles.inlineFacts}>
        <div>
          <dt>議論テーブルのストリーム</dt>
          <dd>{metricValue(metrics, "debate_stream_enabled")}</dd>
        </div>
        <div>
          <dt>ストリームの読取形式</dt>
          <dd>{metricValue(metrics, "debate_stream_view_type")}</dd>
        </div>
      </dl>
    </>
  );
}

function LambdaMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- A horizontally scrollable data table needs keyboard focus.
    <section className={adminStyles.tableScroller} aria-label="Lambda関数状態" tabIndex={0}>
      <table className={`${adminStyles.resourceTable} ${adminStyles.lambdaTable}`}>
        <thead>
          <tr>
            <th scope="col">処理</th>
            <th scope="col">状態</th>
            <th scope="col">更新</th>
            <th scope="col">呼出</th>
            <th scope="col">エラー</th>
            <th scope="col">抑制</th>
            <th scope="col">p95処理時間</th>
          </tr>
        </thead>
        <tbody>
          {LAMBDA_RESOURCES.map((resource) => (
            <tr key={resource.key}>
              <th scope="row">{resource.label}</th>
              <td>{metricValue(metrics, `${resource.key}_state`)}</td>
              <td>{metricValue(metrics, `${resource.key}_update`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_invocations`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_errors`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_throttles`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_duration`)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/* oxlint-disable jsx-a11y/no-noninteractive-tabindex -- Horizontally scrollable data tables need keyboard focus. */
function ApiGatewayMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <section className={adminStyles.tableScroller} aria-label="API Gateway状態" tabIndex={0}>
      <table className={`${adminStyles.resourceTable} ${adminStyles.wideTable}`}>
        <thead>
          <tr>
            <th scope="col">API</th>
            <th scope="col">方式</th>
            <th scope="col">自動反映</th>
            <th scope="col">1時間の呼出</th>
            <th scope="col">4xx</th>
            <th scope="col">5xx</th>
            <th scope="col">p95応答</th>
            <th scope="col">p95連携</th>
          </tr>
        </thead>
        <tbody>
          {API_RESOURCES.map((resource) => (
            <tr key={resource.key}>
              <th scope="row">{resource.label}</th>
              <td>{metricValue(metrics, `${resource.key}_protocol`)}</td>
              <td>{metricValue(metrics, `${resource.key}_auto_deploy`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_requests`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_4xx`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_5xx`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_latency`)}</td>
              <td>{metricValue(metrics, `${resource.key}_hour_integration_latency`)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function EventBridgeMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <>
      <section
        className={adminStyles.tableScroller}
        aria-label="定期実行とイベント配信"
        tabIndex={0}
      >
        <table className={`${adminStyles.resourceTable} ${adminStyles.wideTable}`}>
          <thead>
            <tr>
              <th scope="col">処理</th>
              <th scope="col">状態</th>
              <th scope="col">実行条件</th>
              <th scope="col">24時間の実行</th>
              <th scope="col">24時間の失敗</th>
            </tr>
          </thead>
          <tbody>
            {EVENT_RESOURCES.map((resource) => (
              <tr key={resource.key}>
                <th scope="row">{resource.label}</th>
                <td>{metricValue(metrics, `${resource.key}_state`)}</td>
                <td>{metricValue(metrics, `${resource.key}_expression`)}</td>
                <td>
                  {resource.hasDeliveryMetrics
                    ? metricValue(metrics, `${resource.key}_day_invocations`)
                    : "対象外"}
                </td>
                <td>
                  {resource.hasDeliveryMetrics
                    ? metricValue(metrics, `${resource.key}_day_failures`)
                    : "対象外"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <dl className={adminStyles.inlineFacts}>
        <div>
          <dt>Runtime調整の最大再試行</dt>
          <dd>{metricValue(metrics, "runtime_retry_attempts")}回</dd>
        </div>
      </dl>
    </>
  );
}

function CloudFormationMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <section
      className={adminStyles.tableScroller}
      aria-label="CloudFormation Stack状態"
      tabIndex={0}
    >
      <table className={`${adminStyles.resourceTable} ${adminStyles.wideTable}`}>
        <thead>
          <tr>
            <th scope="col">管理領域</th>
            <th scope="col">Stack状態</th>
            <th scope="col">差分</th>
            <th scope="col">削除保護</th>
            <th scope="col">最終更新</th>
          </tr>
        </thead>
        <tbody>
          {STACK_RESOURCES.map((resource) => (
            <tr key={resource.key}>
              <th scope="row">{resource.label}</th>
              <td>{metricValue(metrics, `${resource.key}_status`)}</td>
              <td>{metricValue(metrics, `${resource.key}_drift`)}</td>
              <td>{metricValue(metrics, `${resource.key}_termination_protection`)}</td>
              <td>{metricValue(metrics, `${resource.key}_updated_at`)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function SsmMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <>
      <dl className={adminStyles.readinessGrid}>
        {CONFIGURATION_GROUPS.map((group) => {
          const ready = metricValue(metrics, `${group.key}_ready`);
          const required = metricValue(metrics, `${group.key}_required`);
          return (
            <div key={group.key}>
              <dt>{group.label}</dt>
              <dd>
                <strong>{ready}</strong>
                <span>／ {required} 項目</span>
              </dd>
            </div>
          );
        })}
      </dl>
      <dl className={adminStyles.inlineFacts}>
        <div>
          <dt>Runtime prompt切替</dt>
          <dd>{metricValue(metrics, "runtime_prompt_pointer_present")}</dd>
        </div>
        <div>
          <dt>設定の最終更新</dt>
          <dd>{metricValue(metrics, "latest_modified_at")}</dd>
        </div>
      </dl>
    </>
  );
}

function CostGovernanceMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <>
      <section className={adminStyles.tableScroller} aria-label="予算状態" tabIndex={0}>
        <table className={adminStyles.resourceTable}>
          <thead>
            <tr>
              <th scope="col">予算</th>
              <th scope="col">実績使用率</th>
              <th scope="col">予測使用率</th>
              <th scope="col">状態</th>
            </tr>
          </thead>
          <tbody>
            {[
              { key: "project", label: "シッテムの箱" },
              { key: "account", label: "AWSアカウント" },
            ].map((budget) => (
              <tr key={budget.key}>
                <th scope="row">{budget.label}</th>
                <td>{metricValue(metrics, `${budget.key}_actual_percent`)}</td>
                <td>{metricValue(metrics, `${budget.key}_forecast_percent`)}</td>
                <td>{metricValue(metrics, `${budget.key}_health`)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <dl className={adminStyles.inlineFacts}>
        <div>
          <dt>費用異常通知</dt>
          <dd>{metricValue(metrics, "anomaly_subscription")}</dd>
        </div>
        <div>
          <dt>通知頻度</dt>
          <dd>{metricValue(metrics, "anomaly_frequency")}</dd>
        </div>
        <div>
          <dt>確認済み通知先</dt>
          <dd>
            {metricValue(metrics, "anomaly_confirmed_subscribers")}／
            {metricValue(metrics, "anomaly_subscribers")}
          </dd>
        </div>
      </dl>
    </>
  );
}

function ExternalMetrics({
  metrics: source,
}: {
  readonly metrics: readonly AdminStatusMetric[];
}): React.JSX.Element {
  const metrics = metricLookup(source);
  return (
    <section className={adminStyles.tableScroller} aria-label="外部集計状態" tabIndex={0}>
      <table className={`${adminStyles.resourceTable} ${adminStyles.wideTable}`}>
        <thead>
          <tr>
            <th scope="col">取得元</th>
            <th scope="col">初期取込</th>
            <th scope="col">鮮度</th>
            <th scope="col">最終成功</th>
            <th scope="col">最終失敗</th>
            <th scope="col">失敗区分</th>
          </tr>
        </thead>
        <tbody>
          {EXTERNAL_SOURCES.map((source) => (
            <tr key={source.key}>
              <th scope="row">{source.label}</th>
              <td>{metricValue(metrics, `${source.key}_initial_complete`)}</td>
              <td>{metricValue(metrics, `${source.key}_fresh`)}</td>
              <td>{metricValue(metrics, `${source.key}_last_success_at`)}</td>
              <td>{metricValue(metrics, `${source.key}_last_failure_at`)}</td>
              <td>{metricValue(metrics, `${source.key}_failure_code`)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
/* oxlint-enable jsx-a11y/no-noninteractive-tabindex */

function ServiceMetrics({ section }: { readonly section: AdminStatusSection }): React.JSX.Element {
  if (section.service === "ecs") return <EcsMetrics metrics={section.metrics} />;
  if (section.service === "ecr") return <EcrMetrics metrics={section.metrics} />;
  if (section.service === "s3") return <S3Metrics metrics={section.metrics} />;
  if (section.service === "dynamodb") return <DynamoDbMetrics metrics={section.metrics} />;
  if (section.service === "lambda") return <LambdaMetrics metrics={section.metrics} />;
  if (section.service === "apigateway") return <ApiGatewayMetrics metrics={section.metrics} />;
  if (section.service === "eventbridge") return <EventBridgeMetrics metrics={section.metrics} />;
  if (section.service === "cloudformation") {
    return <CloudFormationMetrics metrics={section.metrics} />;
  }
  if (section.service === "ssm") return <SsmMetrics metrics={section.metrics} />;
  if (section.service === "cost_governance") {
    return <CostGovernanceMetrics metrics={section.metrics} />;
  }
  if (section.service === "external") return <ExternalMetrics metrics={section.metrics} />;
  return <SimpleMetricGrid section={section} />;
}

function ServiceCard({ section }: { readonly section: AdminStatusSection }): React.JSX.Element {
  const presentation = SERVICE_PRESENTATION[section.service];
  const wide =
    section.service === "ecs" ||
    section.service === "ecr" ||
    section.service === "inspector" ||
    section.service === "s3" ||
    section.service === "dynamodb" ||
    section.service === "lambda" ||
    section.service === "apigateway" ||
    section.service === "eventbridge" ||
    section.service === "cloudformation" ||
    section.service === "ssm" ||
    section.service === "cost_governance" ||
    section.service === "external";
  const summary =
    section.service === "ecs" && section.summary === "IDLE"
      ? "Scale-to-Zeroで待機しています。"
      : section.summary;
  return (
    <article
      className={adminStyles.statusCard}
      data-layout={wide ? "wide" : "standard"}
      data-service={section.service}
    >
      <header className={adminStyles.statusCardHeader}>
        <div className={adminStyles.serviceIdentity}>
          <span className={adminStyles.serviceIcon} data-service={section.service}>
            <ServiceIcon service={section.service} />
          </span>
          <div>
            <h3>
              {presentation.name.split(/(\d+)/u).map((part, index) =>
                /^\d+$/u.test(part) ? (
                  <span
                    className={
                      section.service === "s3"
                        ? adminStyles.displayNumericGlyph
                        : adminStyles.numericGlyph
                    }
                    key={`${part}-${index}`}
                  >
                    {part}
                  </span>
                ) : (
                  part
                ),
              )}
            </h3>
            <span>{presentation.purpose}</span>
          </div>
        </div>
        <StateBadge state={section.state} />
      </header>
      <p className={`${adminStyles.serviceSummary} ${commonStyles.japaneseText}`}>{summary}</p>
      <ServiceMetrics section={section} />
    </article>
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
        </div>
        <div className={adminStyles.panelActions}>
          <span className={adminStyles.readOnlyBadge}>
            <span aria-hidden="true">◇</span>
            閲覧専用
          </span>
          <button
            className={`${commonStyles.secondaryButton} ${adminStyles.refreshButton}`}
            type="button"
            disabled={refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            <span aria-hidden="true">↻</span>
            {refresh.isPending ? "更新しています" : "状態を更新"}
          </button>
        </div>
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
                ? "取得済み情報の有効期限を過ぎています。"
                : "一部のサービスを確認できませんでした。取得できた状態だけを表示しています。"}
            </output>
          )}
          <div className={adminStyles.statusGrid}>
            {data.sections.map((section) => (
              <ServiceCard key={section.service} section={section} />
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
      <header className={`${commonStyles.pageHeader} ${routeStyles.routeMotionItem}`}>
        <p className={commonStyles.eyebrow} lang="en">
          SHITTIM CHEST CONTROL
        </p>
        <h1
          className={`${commonStyles.japaneseText} ${commonStyles.japaneseHeading}`}
          tabIndex={-1}
        >
          管理コンソール
        </h1>
      </header>
      <div className={adminStyles.adminWorkspace}>
        <nav className={adminStyles.sectionNavigation} aria-label="管理画面内ナビゲーション">
          <a href="#admin-overview">概要</a>
          <a href="#admin-status">サービス</a>
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
