import {
  QueryClient,
  QueryClientProvider,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  BrowserRouter,
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
  useParams,
} from "react-router-dom";

import {
  getRecord,
  getRankings,
  getRecords,
  getSession,
  logout,
  RecordsApiError,
  type ParticipantSlot,
  type RankingEntry,
  type RecordDetailResponse,
  type SessionResponse,
  type SortOrder,
} from "./api";
import {
  Avatar,
  AvatarSelect,
  type AvatarSelectOption,
  BrandMark,
  DebateCard,
  ErrorPanel,
  formatCompletedDateTime,
  Layout,
} from "./components";
import { VoteGraph } from "./VoteGraph";
import styles from "./App.module.css";

const SESSION_QUERY_KEY = ["records-session"] as const;
const LOGIN_TRANSITION_KEY = "shittim-records-login-transition";
const JAPANESE_HEADING_CLASS = `${styles.japaneseText} ${styles.japaneseHeading}`;
const JAPANESE_PROSE_CLASS = `${styles.japaneseText} ${styles.japaneseProse}`;
const READABLE_JAPANESE_PROSE_CLASS = `${JAPANESE_PROSE_CLASS} ${styles.readableMeasure}`;

function useAuthenticationRecovery(error: unknown) {
  const client = useQueryClient();
  useEffect(() => {
    if (
      !(error instanceof RecordsApiError) ||
      error.status !== 401 ||
      error.code !== "AUTHENTICATION_REQUIRED"
    ) {
      return;
    }
    void client.invalidateQueries({ queryKey: SESSION_QUERY_KEY, exact: true }).finally(() => {
      client.removeQueries({
        predicate: (query) =>
          query.queryKey[0] === "records" ||
          query.queryKey[0] === "record" ||
          query.queryKey[0] === "rankings",
      });
    });
  }, [client, error]);
}

function LoadingScreen() {
  return (
    <main className={styles.loadingScreen} aria-busy="true">
      <BrandMark />
      <p>記録庫を開いています</p>
    </main>
  );
}

function LoginPage({ session }: { readonly session: SessionResponse }) {
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
    /^\/records\/[A-Za-z0-9_-]{43}$/.test(requestedPath)
      ? requestedPath
      : "/";
  const startPath = `/api/v1/auth/discord/start?returnTo=${encodeURIComponent(returnTo)}`;
  return (
    <main className={styles.loginShell}>
      <div className={styles.backgroundGrid} aria-hidden="true" />
      <section className={styles.loginVisual} aria-hidden="true">
        <BrandMark />
        <div className={styles.orbit}>
          <span />
          <span />
          <span />
        </div>
      </section>
      <section className={styles.loginPanel} aria-labelledby="login-title">
        <h1
          id="login-title"
          className={`${styles.productName} ${styles.loginProductName}`}
          aria-label="The Shittim Chest Archive"
          lang="en"
        >
          <span className={styles.productNameLine}>THE SHITTIM</span>
          <span className={styles.productNameLine}>CHEST ARCHIVE</span>
        </h1>
        <p className={JAPANESE_PROSE_CLASS}>シッテムの箱 議事録閲覧システム</p>
        <a
          className={`${styles.primaryButton} ${styles.loginAuthButton}`}
          href={startPath}
          lang="en"
          onClick={() => sessionStorage.setItem(LOGIN_TRANSITION_KEY, "pending")}
        >
          AUTHENTICATE
        </a>
        <p className={`${styles.loginNote} ${JAPANESE_PROSE_CLASS}`}>
          吹雪型JCのつどいサーバの先生であることを認証します。
        </p>
      </section>
    </main>
  );
}

function BrandTransition({ onComplete }: { readonly onComplete: () => void }) {
  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timer = window.setTimeout(onComplete, reduced ? 150 : 2_000);
    return () => window.clearTimeout(timer);
  }, [onComplete]);
  return (
    <output className={styles.brandTransition} aria-label="ログインしました">
      <BrandMark />
      <p lang="en">WELCOME, SENSEI.</p>
    </output>
  );
}

function AuthenticatedRoutes({
  session,
}: {
  readonly session: SessionResponse & { authenticated: true };
}) {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [showTransition, setShowTransition] = useState(
    () => sessionStorage.getItem(LOGIN_TRANSITION_KEY) === "pending",
  );
  const logoutMutation = useMutation({
    mutationFn: () => logout(session.csrfToken),
    onSuccess: async () => {
      sessionStorage.removeItem(LOGIN_TRANSITION_KEY);
      client.clear();
      await client.invalidateQueries({ queryKey: SESSION_QUERY_KEY });
      void navigate("/login", { replace: true });
    },
  });
  const finishTransition = () => {
    sessionStorage.removeItem(LOGIN_TRANSITION_KEY);
    setShowTransition(false);
  };
  if (showTransition) {
    return <BrandTransition onComplete={finishTransition} />;
  }
  return (
    <Layout
      displayName={session.user.displayName}
      avatar={session.user.avatar}
      onLogout={() => logoutMutation.mutate()}
    >
      <Routes>
        <Route path="/" element={<RecordsHome />} />
        <Route path="/records/:recordId" element={<RecordDetail />} />
        <Route path="/insights" element={<RankingsPage />} />
        <Route path="/login" element={<Navigate to="/" replace />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Layout>
  );
}

function RecordsHome() {
  const [winner, setWinner] = useState<ParticipantSlot | "">("");
  const [sort, setSort] = useState<SortOrder>("newest");
  const [search, setSearch] = useState("");
  const [requester, setRequester] = useState("");
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const localFiltersActive = search.trim().length > 0 || requester !== "";
  const localFiltersActiveRef = useRef(localFiltersActive);
  localFiltersActiveRef.current = localFiltersActive;
  const records = useInfiniteQuery({
    queryKey: ["records", winner, sort],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      getRecords({
        cursor: pageParam,
        sort,
        winner: winner || undefined,
      }),
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  });
  const { fetchNextPage, hasNextPage, isFetchingNextPage, isFetchNextPageError } = records;
  useAuthenticationRecovery(records.error);
  const loadedRecords = useMemo(
    () => records.data?.pages.flatMap((page) => page.items) ?? [],
    [records.data],
  );
  const requesterOptions = useMemo<readonly AvatarSelectOption<string>[]>(() => {
    const requesters = new Map(
      loadedRecords.map((record) => [record.requester.displayName, record.requester] as const),
    );
    return [
      { value: "", label: "すべて", avatar: null },
      ...Array.from(requesters.values())
        .sort((a, b) => a.displayName.localeCompare(b.displayName, "ja-JP"))
        .map((item) => ({ value: item.displayName, label: item.displayName, avatar: item.avatar })),
    ];
  }, [loadedRecords]);
  const requesterNames = useMemo(
    () => requesterOptions.filter((option) => option.value).map((option) => option.value),
    [requesterOptions],
  );
  const winnerOptions = useMemo<readonly AvatarSelectOption<ParticipantSlot | "">[]>(() => {
    const participants = new Map(
      loadedRecords.flatMap((record) =>
        record.participants.map((participant) => [participant.slot, participant] as const),
      ),
    );
    const defaults = {
      "participant-a": { displayName: "アロナ", fallbackVariant: "cyan" },
      "participant-b": { displayName: "プラナ", fallbackVariant: "pink" },
      "participant-c": { displayName: "安倍晋三AI", fallbackVariant: "lavender" },
    } as const;
    return [
      { value: "", label: "すべて", avatar: null },
      ...Object.entries(defaults).map(([slot, fallback]) => {
        const participantSlot = slot as ParticipantSlot;
        const participant = participants.get(participantSlot);
        return {
          value: participantSlot,
          label: participant?.displayName ?? fallback.displayName,
          avatar: participant?.avatar ?? {
            kind: "placeholder" as const,
            url: null,
            alt: `${fallback.displayName}のアバター`,
            fallbackVariant: fallback.fallbackVariant,
          },
        };
      }),
    ];
  }, [loadedRecords]);
  useEffect(() => {
    if (requester && !records.isPending && !requesterNames.includes(requester)) {
      setRequester("");
    }
  }, [records.isPending, requester, requesterNames]);
  const visibleRecords = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase("ja-JP");
    return loadedRecords.filter((record) => {
      if (requester && record.requester.displayName !== requester) return false;
      if (!needle) return true;
      const winnerName = record.participants.find(
        (participant) => participant.slot === record.result.winner,
      )?.displayName;
      return [record.questionPreview, winnerName]
        .filter(Boolean)
        .some((value) => value?.toLocaleLowerCase("ja-JP").includes(needle));
    });
  }, [loadedRecords, requester, search]);
  useEffect(() => {
    const sentinel = loadMoreRef.current;
    if (
      !sentinel ||
      !hasNextPage ||
      isFetchingNextPage ||
      isFetchNextPageError ||
      localFiltersActive ||
      !("IntersectionObserver" in window)
    ) {
      return;
    }
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry?.isIntersecting || localFiltersActiveRef.current) return;
        observer.unobserve(sentinel);
        void fetchNextPage();
      },
      { rootMargin: "320px 0px", threshold: 0 },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage, isFetchNextPageError, localFiltersActive]);
  const error = records.error instanceof RecordsApiError ? records.error : undefined;
  return (
    <>
      <header className={styles.pageHeader}>
        <p className={styles.eyebrow} lang="en">
          RECORDS ARCHIVE
        </p>
        <h1 className={JAPANESE_HEADING_CLASS} tabIndex={-1}>
          議論の記録
        </h1>
        <p className={JAPANESE_PROSE_CLASS}>議論記録を閲覧できます。</p>
      </header>
      <section className={styles.filters} aria-label="記録の絞り込み">
        <label>
          フリーワード検索
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="質問文などを入力"
          />
        </label>
        <AvatarSelect
          label="依頼者"
          value={requester}
          options={requesterOptions}
          onChange={setRequester}
        />
        <AvatarSelect label="勝者" value={winner} options={winnerOptions} onChange={setWinner} />
        <label>
          並び順
          <select value={sort} onChange={(event) => setSort(event.target.value as SortOrder)}>
            <option value="newest">新しい順</option>
            <option value="oldest">古い順</option>
          </select>
        </label>
        <p>検索対象は現在読み込み済みのカードです。</p>
      </section>
      {records.isPending && (
        <p className={styles.loadingLine} aria-live="polite">
          記録を読み込んでいます。
        </p>
      )}
      {records.isError && (
        <ErrorPanel
          title="記録を読み込めませんでした"
          message={error?.message ?? "通信状態を確認してください。"}
          requestId={error?.requestId}
          onRetry={() => void records.refetch()}
        />
      )}
      {!records.isPending && !records.isError && visibleRecords.length === 0 && (
        <section className={styles.emptyState}>
          <span aria-hidden="true">◇</span>
          <h2>該当する記録はありません</h2>
          <p>条件を変更して、もう一度探してみてください。</p>
        </section>
      )}
      <section className={styles.cardGrid} aria-label="完了した議論">
        {visibleRecords.map((record) => (
          <DebateCard key={record.recordId} record={record} />
        ))}
      </section>
      {records.hasNextPage && (
        <div
          className={styles.loadMore}
          ref={loadMoreRef}
          aria-busy={isFetchingNextPage}
          aria-live="polite"
        >
          {isFetchingNextPage ? (
            <span>次の記録を読み込んでいます。</span>
          ) : isFetchNextPageError ? (
            <button
              className={styles.secondaryButton}
              type="button"
              onClick={() => void fetchNextPage()}
            >
              次の記録をもう一度読み込む
            </button>
          ) : localFiltersActive ? (
            <button
              className={styles.secondaryButton}
              type="button"
              onClick={() => void fetchNextPage()}
            >
              検索対象をさらに読み込む
            </button>
          ) : "IntersectionObserver" in window ? (
            <span>この位置までスクロールすると、次の記録を自動で読み込みます。</span>
          ) : (
            <button
              className={styles.secondaryButton}
              type="button"
              onClick={() => void fetchNextPage()}
            >
              さらに読み込む
            </button>
          )}
        </div>
      )}
    </>
  );
}

function RecordDetail() {
  const { recordId = "" } = useParams();
  const record = useQuery({
    queryKey: ["record", recordId],
    queryFn: () => getRecord(recordId),
    enabled: /^[A-Za-z0-9_-]{43}$/.test(recordId),
  });
  useAuthenticationRecovery(record.error);
  if (!/^[A-Za-z0-9_-]{43}$/.test(recordId)) return <NotFound />;
  if (record.isPending) return <p className={styles.loadingLine}>議論の記録を開いています。</p>;
  if (record.isError) {
    const error = record.error instanceof RecordsApiError ? record.error : undefined;
    return (
      <ErrorPanel
        title="記録を開けませんでした"
        message={error?.message ?? "通信状態を確認してください。"}
        requestId={error?.requestId}
        onRetry={() => void record.refetch()}
      />
    );
  }
  return <RecordDocument record={record.data} />;
}

function RecordDocument({ record }: { readonly record: RecordDetailResponse }) {
  const participant = (slot: ParticipantSlot) =>
    record.participants.find((item) => item.slot === slot)!;
  const count = (slot: ParticipantSlot) =>
    record.result.voteCounts.find((item) => item.participant === slot)?.count ?? 0;
  return (
    <article className={styles.recordDocument}>
      <header className={styles.recordHeader}>
        <Link className={styles.backLink} to="/">
          ← 記録一覧へ
        </Link>
        <p className={styles.eyebrow} lang="en">
          COMPLETED DEBATE
        </p>
        <h1 className={JAPANESE_HEADING_CLASS} tabIndex={-1}>
          {record.question}
        </h1>
        <div className={styles.recordMeta}>
          <Avatar avatar={record.requester.avatar} />
          <span>
            <small>依頼者</small>
            {record.requester.displayName}
          </span>
          <time dateTime={record.completedAt}>{formatCompletedDateTime(record.completedAt)}</time>
        </div>
      </header>
      <section className={styles.detailSection} aria-labelledby="opinions-title">
        <h2 id="opinions-title" className={JAPANESE_HEADING_CLASS}>
          3人の意見
        </h2>
        <div className={styles.opinionGrid}>
          {record.participants.map((person) => {
            const initial = record.initialOpinions.find(
              (item) => item.participant === person.slot,
            )!;
            const final = record.finalProposals.find((item) => item.participant === person.slot)!;
            return (
              <article className={styles.opinionCard} key={person.slot}>
                <header>
                  <Avatar avatar={person.avatar} />
                  <h3>{person.displayName}</h3>
                </header>
                <div>
                  <h4>初回意見</h4>
                  <strong className={styles.japaneseText}>{initial.summary}</strong>
                  <p className={JAPANESE_PROSE_CLASS}>{initial.proposal}</p>
                </div>
                <div className={styles.finalProposal}>
                  <h4>最終案</h4>
                  <strong className={styles.japaneseText}>{final.title}</strong>
                  <p className={JAPANESE_PROSE_CLASS}>{final.proposal}</p>
                </div>
              </article>
            );
          })}
        </div>
      </section>
      <section className={styles.detailSection} aria-labelledby="votes-title">
        <h2 id="votes-title" className={JAPANESE_HEADING_CLASS}>
          投票
        </h2>
        <VoteGraph record={record} />
        <div className={styles.voteList}>
          {record.votes.map((vote) => (
            <article key={vote.voter} className={styles.voteCard}>
              <Avatar avatar={participant(vote.voter).avatar} />
              <div>
                <h3>
                  {participant(vote.voter).displayName} → {participant(vote.candidate).displayName}
                </h3>
                <p className={JAPANESE_PROSE_CLASS}>{vote.reason}</p>
              </div>
            </article>
          ))}
        </div>
        {record.result.tieBreakApplied && (
          <p className={`${styles.tieNotice} ${JAPANESE_PROSE_CLASS}`}>
            同票のため、シッテムの箱の既定ルールで勝者を決定しました。
          </p>
        )}
      </section>
      <section
        className={`${styles.detailSection} ${styles.decisionSection}`}
        aria-labelledby="decision-title"
      >
        <p className={styles.eyebrow} lang="en">
          FINAL DECISION
        </p>
        <h2 id="decision-title" className={JAPANESE_HEADING_CLASS}>
          最終決定
        </h2>
        <div className={styles.winnerPanel}>
          <Avatar avatar={participant(record.result.winner).avatar} />
          <div>
            <small>勝者 {count(record.result.winner)}票</small>
            <h3>{participant(record.result.winner).displayName}</h3>
            {record.finalDecision.victoryMessage && (
              <blockquote className={READABLE_JAPANESE_PROSE_CLASS}>
                {record.finalDecision.victoryMessage}
              </blockquote>
            )}
          </div>
        </div>
        <p className={`${styles.decisionText} ${READABLE_JAPANESE_PROSE_CLASS}`}>
          {record.finalDecision.decision}
        </p>
        <div className={styles.decisionColumns}>
          <div>
            <h3>実行案</h3>
            <ul className={JAPANESE_PROSE_CLASS}>
              {record.finalDecision.actions.map((action) => (
                <li key={action}>{action}</li>
              ))}
            </ul>
          </div>
          <div>
            <h3>注意点</h3>
            <ul className={JAPANESE_PROSE_CLASS}>
              {record.finalDecision.caveats.map((caveat) => (
                <li key={caveat}>{caveat}</li>
              ))}
            </ul>
          </div>
        </div>
      </section>
    </article>
  );
}

function RankingPanel({
  variant,
  title,
  description,
  entries,
  pending,
  error,
  onRetry,
}: {
  readonly variant: "wins" | "requests";
  readonly title: string;
  readonly description: string;
  readonly entries: readonly RankingEntry[] | undefined;
  readonly pending: boolean;
  readonly error: unknown;
  readonly onRetry: () => void;
}) {
  const apiError = error instanceof RecordsApiError ? error : undefined;
  const preparing = apiError?.status === 503 && apiError.code === "INSIGHTS_UNAVAILABLE";
  const scaleMaximum = Math.max(1, ...(entries?.map((entry) => entry.count) ?? []));
  const total = entries?.reduce((sum, entry) => sum + entry.count, 0) ?? 0;
  return (
    <section
      className={`${styles.rankingPanel} ${
        variant === "wins" ? styles.rankingPanelWins : styles.rankingPanelRequests
      }`}
      aria-labelledby={`${title}-title`}
      aria-busy={pending}
    >
      <header className={styles.rankingHeader}>
        <div className={styles.rankingHeaderLayout}>
          <RankingEmblem variant={variant} />
          <div className={styles.rankingHeaderCopy}>
            <p className={styles.eyebrow} lang="en">
              {variant === "wins" ? "VICTORIES" : "REQUESTS"}
            </p>
            <h2 id={`${title}-title`} className={JAPANESE_HEADING_CLASS}>
              {title}
            </h2>
          </div>
          {!pending && !error && entries && entries.length > 0 && (
            <output
              aria-label={`${title}の${variant === "wins" ? "合計" : "上位合計"}`}
              className={styles.rankingTotal}
            >
              <span>{variant === "wins" ? "合計" : "上位合計"}</span>
              <strong>{total}</strong>回
            </output>
          )}
        </div>
        <p className={`${JAPANESE_PROSE_CLASS} ${styles.rankingDescription}`}>{description}</p>
      </header>
      {pending && (
        <p className={styles.rankingStatus} aria-live="polite">
          集計結果を読み込んでいます。
        </p>
      )}
      {preparing && (
        <output className={styles.rankingStatus}>
          <strong>集計を準備しています</strong>
          <span>最初の集計が終わるまで、しばらくお待ちください。</span>
        </output>
      )}
      {error !== null && error !== undefined && !preparing && (
        <div className={styles.rankingStatus} role="alert">
          <strong>ランキングを読み込めませんでした</strong>
          <span>{apiError?.message ?? "通信状態を確認してください。"}</span>
          <button className={styles.secondaryButton} type="button" onClick={onRetry}>
            もう一度試す
          </button>
        </div>
      )}
      {!pending && !error && entries?.length === 0 && (
        <div className={styles.rankingStatus}>
          <strong>まだ集計対象がありません</strong>
          <span>完了した議論が記録されると、ここに表示されます。</span>
        </div>
      )}
      {!pending && !error && entries && entries.length > 0 && (
        <>
          {variant === "wins" && <WinPodium entries={entries} total={total} />}
          <ol
            className={`${styles.rankingList} ${
              variant === "wins" ? styles.winRankingList : styles.requestRankingList
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
                  className={entry.rank <= 3 ? styles[`rankingTop${entry.rank}`] : undefined}
                  key={`${entry.rank}-${entry.displayName}-${index}`}
                  value={entry.rank}
                >
                  <span className={styles.rankingPosition}>
                    <span aria-hidden="true">{entry.rank}</span>
                    <span className={styles.visuallyHidden}>{entry.rank}位</span>
                  </span>
                  {variant === "requests" ? (
                    <RankingShareAvatar entry={entry} total={total} />
                  ) : (
                    <Avatar avatar={entry.avatar} />
                  )}
                  <span className={styles.rankingName}>{entry.displayName}</span>
                  <span className={styles.rankingCount}>
                    <strong>{entry.count}</strong>回
                  </span>
                  <meter
                    aria-label={meterLabel}
                    className={styles.rankingBar}
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
      className={`${styles.rankingEmblem} ${
        variant === "wins" ? styles.rankingEmblemWins : styles.rankingEmblemRequests
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
      className={`${styles.winPodium} ${!hasUniquePodium ? styles.winPodiumShared : ""}`}
      data-podium-layout={hasUniquePodium ? "ranked" : "shared"}
      aria-hidden="true"
    >
      <span className={styles.podiumOrbit} />
      {placedEntries.map(({ entry, placement }, index) => {
        const share = total > 0 ? Math.round((entry.count / total) * 100) : 0;
        return (
          <div
            className={`${styles.podiumEntry} ${styles[`podium-${placement}`]} ${
              entry.rank <= 3 ? styles[`podiumRank${entry.rank}`] : ""
            }`}
            key={`${entry.rank}-${entry.displayName}-${index}`}
          >
            <span className={styles.podiumBadge}>{entry.rank}位</span>
            <span className={styles.podiumAvatarRing}>
              <Avatar avatar={entry.avatar} />
            </span>
            <strong className={styles.podiumName}>{entry.displayName}</strong>
            <span className={styles.podiumScore}>
              <strong>{entry.count}</strong>回 <small>{share}%</small>
            </span>
            <span className={styles.podiumBase} />
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
    <span className={styles.rankingShareVisual} aria-hidden="true">
      <span
        className={styles.rankingShareRing}
        style={{
          background: `conic-gradient(var(--ranking-bar-end) ${share * 3.6}deg, rgb(23 50 77 / 8%) 0deg)`,
        }}
      >
        <Avatar avatar={entry.avatar} />
      </span>
      <span className={styles.rankingShareLabel}>{share}%</span>
    </span>
  );
}

function RankingsPage() {
  const rankings = useQuery({ queryKey: ["rankings"], queryFn: getRankings });
  useAuthenticationRecovery(rankings.error);
  return (
    <>
      <header className={styles.pageHeader}>
        <p className={styles.eyebrow} lang="en">
          RECORDS INSIGHTS
        </p>
        <h1 className={JAPANESE_HEADING_CLASS} tabIndex={-1}>
          いろいろな記録
        </h1>
        <p className={JAPANESE_PROSE_CLASS}>これまでの議論を、ランキングで振り返れます。</p>
        {rankings.data && (
          <p className={styles.insightsGeneratedAt}>
            最終集計:{" "}
            <time dateTime={rankings.data.generatedAt}>
              {formatCompletedDateTime(rankings.data.generatedAt)}
            </time>
          </p>
        )}
      </header>
      <div className={styles.rankingsGrid}>
        <RankingPanel
          variant="wins"
          title="勝利回数ランキング"
          description="3人の参加者が勝者に選ばれた回数と、全勝利に占める割合です。"
          entries={rankings.data?.wins}
          pending={rankings.isPending}
          error={rankings.error}
          onRetry={() => void rankings.refetch()}
        />
        <RankingPanel
          variant="requests"
          title="依頼回数ランキング"
          description="議論を依頼した回数の上位10人です。リングは表示中の上位合計に占める割合です。"
          entries={rankings.data?.requests}
          pending={rankings.isPending}
          error={rankings.error}
          onRetry={() => void rankings.refetch()}
        />
      </div>
    </>
  );
}

function NotFound() {
  return (
    <section className={styles.messagePanel}>
      <span className={styles.errorRing} aria-hidden="true">
        ?
      </span>
      <h1>ページが見つかりません</h1>
      <p>指定されたページは存在しないか、閲覧できません。</p>
      <Link className={styles.primaryButton} to="/">
        記録一覧へ戻る
      </Link>
    </section>
  );
}

function ApplicationRoutes() {
  const session = useQuery({ queryKey: SESSION_QUERY_KEY, queryFn: getSession });
  const location = useLocation();
  if (session.isPending) return <LoadingScreen />;
  if (session.isError) {
    const error = session.error instanceof RecordsApiError ? session.error : undefined;
    return (
      <main className={styles.errorShell}>
        <ErrorPanel
          title="記録庫へ接続できません"
          message={error?.message ?? "しばらくしてから、もう一度お試しください。"}
          requestId={error?.requestId}
          onRetry={() => void session.refetch()}
        />
      </main>
    );
  }
  if (!session.data.authenticated) {
    if (location.pathname !== "/login") {
      return <Navigate to="/login" state={{ from: location.pathname }} replace />;
    }
    return (
      <Routes>
        <Route path="/login" element={<LoginPage session={session.data} />} />
      </Routes>
    );
  }
  return <AuthenticatedRoutes session={session.data} />;
}

export function App() {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <ApplicationRoutes />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
