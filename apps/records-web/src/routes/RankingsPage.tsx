import { useState } from "react";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { getCosts } from "../api/costs";
import { RecordsApiError } from "../api/http";
import { getRankings } from "../api/rankings";
import type { CostPeriod, CostsResponse, RankingEntry } from "../api/types";
import { Avatar } from "../components/Avatar";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import { formatCompletedDateTime } from "../lib/dateTime";
import { routeMotionDelay } from "../lib/routePresentation";
import commonStyles from "../styles/common.module.css";
import rankingStyles from "../styles/rankings.module.css";
import routeStyles from "../styles/routeMotion.module.css";

const JAPANESE_HEADING_CLASS = `${commonStyles.japaneseText} ${commonStyles.japaneseHeading}`;
const JAPANESE_PROSE_CLASS = `${commonStyles.japaneseText} ${commonStyles.japaneseProse}`;
const COST_PERIODS: readonly { value: CostPeriod; label: string }[] = [
  { value: "today", label: "今日" },
  { value: "week", label: "直近7日" },
  { value: "month", label: "今月" },
  { value: "all", label: "全期間" },
];
const COST_CATEGORIES: readonly {
  key: keyof CostsResponse["breakdown"];
  label: string;
  className: string;
}[] = [
  { key: "fargate", label: "Fargate", className: rankingStyles.costFargate },
  { key: "lambda", label: "Lambda", className: rankingStyles.costLambda },
  { key: "openai", label: "OpenAI", className: rankingStyles.costOpenai },
  { key: "otherAws", label: "その他AWS", className: rankingStyles.costOtherAws },
];

interface RankingPanelProps {
  readonly variant: "wins" | "requests";
  readonly title: string;
  readonly description: string;
  readonly entries: readonly RankingEntry[] | undefined;
  readonly pending: boolean;
  readonly error: unknown;
  readonly onRetry: () => void;
  readonly motionDelay: number;
  readonly motionTerminal?: boolean;
}

function RankingPanel({
  variant,
  title,
  description,
  entries,
  pending,
  error,
  onRetry,
  motionDelay,
  motionTerminal = false,
}: RankingPanelProps) {
  const apiError = error instanceof RecordsApiError ? error : undefined;
  const preparing = apiError?.status === 503 && apiError.code === "INSIGHTS_UNAVAILABLE";
  const scaleMaximum = Math.max(1, ...(entries?.map((entry) => entry.count) ?? []));
  const total = entries?.reduce((sum, entry) => sum + entry.count, 0) ?? 0;

  return (
    <section
      className={`${rankingStyles.rankingPanel} ${routeStyles.routeMotionItem} ${
        variant === "wins" ? rankingStyles.rankingPanelWins : rankingStyles.rankingPanelRequests
      }`}
      data-route-motion-terminal={motionTerminal ? "" : undefined}
      style={routeMotionDelay(motionDelay)}
      aria-labelledby={`${title}-title`}
      aria-busy={pending}
    >
      <header className={rankingStyles.rankingHeader}>
        <div className={rankingStyles.rankingHeaderLayout}>
          <RankingEmblem variant={variant} />
          <div className={rankingStyles.rankingHeaderCopy}>
            <p className={commonStyles.eyebrow} lang="en">
              {variant === "wins" ? "VICTORIES" : "REQUESTS"}
            </p>
            <h2 id={`${title}-title`} className={JAPANESE_HEADING_CLASS}>
              {title}
            </h2>
          </div>
          {!pending && !error && entries && entries.length > 0 && (
            <output
              aria-label={`${title}の${variant === "wins" ? "合計" : "上位合計"}`}
              className={rankingStyles.rankingTotal}
            >
              <span>{variant === "wins" ? "合計" : "上位合計"}</span>
              <strong>{total}</strong>回
            </output>
          )}
        </div>
        <p className={`${JAPANESE_PROSE_CLASS} ${rankingStyles.rankingDescription}`}>
          {description}
        </p>
      </header>
      {pending && (
        <p className={rankingStyles.rankingStatus} aria-live="polite">
          集計結果を読み込んでいます。
        </p>
      )}
      {preparing && (
        <output className={rankingStyles.rankingStatus}>
          <strong>集計を準備しています</strong>
          <span>最初の集計が終わるまで、しばらくお待ちください。</span>
        </output>
      )}
      {error !== null && error !== undefined && !preparing && (
        <div className={rankingStyles.rankingStatus} role="alert">
          <strong>ランキングを読み込めませんでした</strong>
          <span>{apiError?.message ?? "通信状態を確認してください。"}</span>
          <button className={commonStyles.secondaryButton} type="button" onClick={onRetry}>
            もう一度試す
          </button>
        </div>
      )}
      {!pending && !error && entries?.length === 0 && (
        <div className={rankingStyles.rankingStatus}>
          <strong>まだ集計対象がありません</strong>
          <span>完了した議論が記録されると、ここに表示されます。</span>
        </div>
      )}
      {!pending && !error && entries && entries.length > 0 && (
        <>
          {variant === "wins" && <WinPodium entries={entries} total={total} />}
          <ol
            className={`${rankingStyles.rankingList} ${
              variant === "wins" ? rankingStyles.winRankingList : rankingStyles.requestRankingList
            }`}
          >
            {entries.map((entry, index) => {
              const share = total > 0 ? Math.round((entry.count / total) * 100) : 0;
              const meterLabel =
                variant === "requests"
                  ? `${entry.displayName}: ${entry.count}回（上位合計の${share}%、最多${scaleMaximum}回との比較）`
                  : `${entry.displayName}: ${entry.count}回（最多${scaleMaximum}回との比較）`;
              return (
                <li
                  className={entry.rank <= 3 ? rankingStyles[`rankingTop${entry.rank}`] : undefined}
                  key={`${entry.rank}-${entry.displayName}-${index}`}
                  value={entry.rank}
                >
                  <span className={rankingStyles.rankingPosition}>
                    <span aria-hidden="true">{entry.rank}</span>
                    <span className={commonStyles.visuallyHidden}>{entry.rank}位</span>
                  </span>
                  {variant === "requests" ? (
                    <RankingShareAvatar entry={entry} total={total} />
                  ) : (
                    <Avatar avatar={entry.avatar} />
                  )}
                  <span className={rankingStyles.rankingName}>{entry.displayName}</span>
                  <span className={rankingStyles.rankingCount}>
                    <strong>{entry.count}</strong>回
                  </span>
                  <meter
                    aria-label={meterLabel}
                    className={rankingStyles.rankingBar}
                    max={scaleMaximum}
                    min={0}
                    value={entry.count}
                  />
                </li>
              );
            })}
          </ol>
        </>
      )}
    </section>
  );
}

function RankingEmblem({ variant }: { readonly variant: "wins" | "requests" }) {
  return (
    <span
      className={`${rankingStyles.rankingEmblem} ${
        variant === "wins" ? rankingStyles.rankingEmblemWins : rankingStyles.rankingEmblemRequests
      }`}
      aria-hidden="true"
    >
      {variant === "wins" ? (
        <svg viewBox="0 0 48 48">
          <path d="M9 17l9 8 6-13 6 13 9-8-4 19H13L9 17Z" />
          <path d="M14 40h20" />
        </svg>
      ) : (
        <svg viewBox="0 0 48 48">
          <circle cx="24" cy="20" r="10" />
          <circle cx="24" cy="20" r="4" />
          <path d="m17 29-3 12 10-5 10 5-3-12" />
        </svg>
      )}
    </span>
  );
}

function WinPodium({
  entries,
  total,
}: {
  readonly entries: readonly RankingEntry[];
  readonly total: number;
}) {
  const topThree = entries.slice(0, 3);
  if (topThree.length !== 3) return null;

  const hasUniquePodium = topThree.every((entry, index) => entry.rank === index + 1);
  const placedEntries = hasUniquePodium
    ? [
        { entry: topThree[1]!, placement: "second" },
        { entry: topThree[0]!, placement: "first" },
        { entry: topThree[2]!, placement: "third" },
      ]
    : topThree.map((entry) => ({ entry, placement: "shared" as const }));

  return (
    <div
      className={`${rankingStyles.winPodium} ${
        !hasUniquePodium ? rankingStyles.winPodiumShared : ""
      }`}
      data-podium-layout={hasUniquePodium ? "ranked" : "shared"}
      aria-hidden="true"
    >
      {placedEntries.map(({ entry, placement }, index) => {
        const share = total > 0 ? Math.round((entry.count / total) * 100) : 0;
        return (
          <div
            className={`${rankingStyles.podiumEntry} ${rankingStyles[`podium-${placement}`]} ${
              entry.rank <= 3 ? rankingStyles[`podiumRank${entry.rank}`] : ""
            }`}
            key={`${entry.rank}-${entry.displayName}-${index}`}
          >
            <span className={rankingStyles.podiumBadge}>{entry.rank}位</span>
            <span className={rankingStyles.podiumAvatarRing}>
              <Avatar avatar={entry.avatar} />
            </span>
            <strong className={rankingStyles.podiumName}>{entry.displayName}</strong>
            <span className={rankingStyles.podiumScore}>
              <strong>{entry.count}</strong>回 <small>{share}%</small>
            </span>
            <span className={rankingStyles.podiumBase} />
          </div>
        );
      })}
    </div>
  );
}

function RankingShareAvatar({
  entry,
  total,
}: {
  readonly entry: RankingEntry;
  readonly total: number;
}) {
  const share = total > 0 ? Math.round((entry.count / total) * 100) : 0;
  return (
    <span className={rankingStyles.rankingShareVisual} aria-hidden="true">
      <span
        className={rankingStyles.rankingShareRing}
        style={{
          background: `conic-gradient(var(--ranking-bar-end) ${share * 3.6}deg, var(--records-meter-track) 0deg)`,
        }}
      >
        <Avatar avatar={entry.avatar} />
      </span>
      <span className={rankingStyles.rankingShareLabel}>{share}%</span>
    </span>
  );
}

function CostDashboard({
  costs,
  period,
  onPeriodChange,
}: {
  readonly costs: UseQueryResult<CostsResponse>;
  readonly period: CostPeriod;
  readonly onPeriodChange: (period: CostPeriod) => void;
}) {
  const apiError = costs.error instanceof RecordsApiError ? costs.error : undefined;
  const values = costs.data?.breakdown;
  const numericTotal = costs.data === undefined ? 0 : Number(costs.data.total);
  const graphTotal = Number.isFinite(numericTotal) && numericTotal > 0 ? numericTotal : 0;

  return (
    <section
      className={[rankingStyles.costPanel, routeStyles.routeMotionItem].join(" ")}
      data-route-motion-terminal=""
      style={routeMotionDelay(120)}
      aria-labelledby="cost-dashboard-title"
      aria-busy={costs.isPending}
    >
      <header className={rankingStyles.costHeader}>
        <div>
          <p className={commonStyles.eyebrow} lang="en">
            ESTIMATED COSTS
          </p>
          <h2 id="cost-dashboard-title" className={JAPANESE_HEADING_CLASS}>
            概算費用
          </h2>
        </div>
        <fieldset className={rankingStyles.costPeriod}>
          <legend className={commonStyles.visuallyHidden}>費用の集計期間</legend>
          {COST_PERIODS.map((option) => (
            <label key={option.value}>
              <input
                type="radio"
                name="cost-period"
                value={option.value}
                checked={period === option.value}
                onChange={() => onPeriodChange(option.value)}
              />
              <span>{option.label}</span>
            </label>
          ))}
        </fieldset>
      </header>

      {costs.isPending && (
        <p className={rankingStyles.costStatus} aria-live="polite">
          費用を読み込んでいます。
        </p>
      )}
      {costs.error && (
        <div className={rankingStyles.costStatus} role="alert">
          <strong>費用を取得できません</strong>
          <span>{apiError?.message ?? "通信状態を確認してください。"}</span>
          <button
            className={commonStyles.secondaryButton}
            type="button"
            onClick={() => void costs.refetch()}
          >
            もう一度試す
          </button>
        </div>
      )}
      {costs.data?.status === "unavailable" && (
        <output className={rankingStyles.costStatus}>
          <strong>費用を取得できません</strong>
          <span>有効な日次換算値がまだありません。</span>
        </output>
      )}
      {costs.data && costs.data.status !== "unavailable" && values && (
        <div className={rankingStyles.costContent}>
          <div className={rankingStyles.costSummary}>
            <span>概算合計</span>
            <strong>{formatJpy(costs.data.total)}</strong>
            <small>
              {formatCalendarDate(costs.data.startDate)}〜{formatCalendarDate(costs.data.endDate)}
              （日本時間）
            </small>
            {costs.data.status === "partial" && <em>一部集計中</em>}
          </div>
          <div className={rankingStyles.costStack} aria-hidden="true">
            {COST_CATEGORIES.map(({ key, label, className }) => {
              const numericValue = Number(values[key]);
              const share =
                graphTotal > 0 && Number.isFinite(numericValue) && numericValue > 0
                  ? (numericValue / graphTotal) * 100
                  : 0;
              return (
                <span
                  className={[rankingStyles.costStackPart, className].join(" ")}
                  style={{ inlineSize: String(share) + "%" }}
                  title={label + ": " + formatJpy(values[key])}
                  key={key}
                />
              );
            })}
          </div>
          <ul className={rankingStyles.costList}>
            {COST_CATEGORIES.map(({ key, label, className }) => (
              <li key={key}>
                <span
                  className={[rankingStyles.costSwatch, className].join(" ")}
                  aria-hidden="true"
                />
                <span>{label}</span>
                <strong>{formatJpy(values[key])}</strong>
              </li>
            ))}
          </ul>
          <footer className={[JAPANESE_PROSE_CLASS, rankingStyles.costNotes].join(" ")}>
            <p>
              概算・日本時間。Frankfurter v2の日次参照rateでUSD原価を円換算しています。
              共有固定費のRoute 53は含みません。
            </p>
            {costs.data.updatedAt && (
              <p>
                最終更新:{" "}
                <time dateTime={costs.data.updatedAt}>
                  {formatCompletedDateTime(costs.data.updatedAt)}
                </time>
              </p>
            )}
          </footer>
        </div>
      )}
    </section>
  );
}

function formatJpy(value: string): string {
  const [integer = "0", fraction] = value.split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return "¥" + grouped + (fraction === undefined ? "" : "." + fraction);
}

function formatCalendarDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) return value;
  return Number(match[1]) + "年" + Number(match[2]) + "月" + Number(match[3]) + "日";
}

export default function RankingsPage() {
  const [period, setPeriod] = useState<CostPeriod>("week");
  const rankings = useQuery({ queryKey: ["rankings"], queryFn: getRankings });
  const costs = useQuery({
    queryKey: ["costs", period],
    queryFn: () => getCosts(period),
  });
  useAuthenticationRecovery(rankings.error);
  useAuthenticationRecovery(costs.error);

  return (
    <>
      <header
        className={`${commonStyles.pageHeader} ${routeStyles.routeMotionItem}`}
        data-route-motion-ready={rankings.isPending ? undefined : ""}
        style={routeMotionDelay(0)}
      >
        <p className={commonStyles.eyebrow} lang="en">
          RECORDS INSIGHTS
        </p>
        <h1 className={JAPANESE_HEADING_CLASS} tabIndex={-1}>
          いろいろな記録
        </h1>
        {rankings.data && (
          <p className={commonStyles.insightsGeneratedAt}>
            最終集計:{" "}
            <time dateTime={rankings.data.generatedAt}>
              {formatCompletedDateTime(rankings.data.generatedAt)}
            </time>
          </p>
        )}
      </header>
      <div className={rankingStyles.rankingsGrid}>
        <RankingPanel
          variant="wins"
          title="勝利回数ランキング"
          description="3人の参加者が勝者に選ばれた回数と、全勝利に占める割合です。"
          entries={rankings.data?.wins}
          pending={rankings.isPending}
          error={rankings.error}
          onRetry={() => void rankings.refetch()}
          motionDelay={60}
        />
        <RankingPanel
          variant="requests"
          title="依頼回数ランキング"
          description="議論を依頼した回数の上位10人です。リングは表示中の上位合計に占める割合です。"
          entries={rankings.data?.requests}
          pending={rankings.isPending}
          error={rankings.error}
          onRetry={() => void rankings.refetch()}
          motionDelay={100}
        />
        <CostDashboard costs={costs} period={period} onPeriodChange={setPeriod} />
      </div>
    </>
  );
}
