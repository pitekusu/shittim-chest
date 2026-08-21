import {
  QueryClient,
  QueryClientProvider,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
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
  getRecords,
  getSession,
  logout,
  RecordsApiError,
  type ParticipantSlot,
  type RecordDetailResponse,
  type SessionResponse,
  type SortOrder,
} from "./api";
import {
  Avatar,
  BrandMark,
  DebateCard,
  ErrorPanel,
  formatCompletedDateTime,
  Layout,
  ProductName,
} from "./components";
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
        predicate: (query) => query.queryKey[0] === "records" || query.queryKey[0] === "record",
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
    requestedPath === "/" || /^\/records\/[A-Za-z0-9_-]{43}$/.test(requestedPath)
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
        <p className={styles.eyebrow}>THE SHITTIM CHEST</p>
        <ProductName headingId="login-title" />
        <p className={JAPANESE_PROSE_CLASS}>
          完了した議論を、あとから静かに振り返るための記録庫です。
        </p>
        <a
          className={styles.primaryButton}
          href={startPath}
          onClick={() => sessionStorage.setItem(LOGIN_TRANSITION_KEY, "pending")}
        >
          Discordでログイン
        </a>
        <p className={`${styles.loginNote} ${JAPANESE_PROSE_CLASS}`}>
          対象Discord Guildのメンバーだけが閲覧できます。
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
      <p>WELCOME, SENSEI.</p>
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
  useAuthenticationRecovery(records.error);
  const loadedRecords = useMemo(
    () => records.data?.pages.flatMap((page) => page.items) ?? [],
    [records.data],
  );
  const requesterNames = useMemo(
    () =>
      Array.from(new Set(loadedRecords.map((record) => record.requester.displayName))).sort(
        (a, b) => a.localeCompare(b, "ja-JP"),
      ),
    [loadedRecords],
  );
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
  const error = records.error instanceof RecordsApiError ? records.error : undefined;
  return (
    <>
      <header className={styles.pageHeader}>
        <p className={styles.eyebrow}>RECORDS ARCHIVE</p>
        <h1 className={JAPANESE_HEADING_CLASS} tabIndex={-1}>
          議論の記録
        </h1>
        <p className={JAPANESE_PROSE_CLASS}>正常に完了した議論だけを、選んだ順序で閲覧できます。</p>
      </header>
      <section className={styles.filters} aria-label="記録の絞り込み">
        <label>
          読み込み済みの記録を検索
          <input
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="質問・勝者"
          />
        </label>
        <label>
          依頼者
          <select value={requester} onChange={(event) => setRequester(event.target.value)}>
            <option value="">すべて</option>
            {requesterNames.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          勝者
          <select
            value={winner}
            onChange={(event) => setWinner(event.target.value as ParticipantSlot | "")}
          >
            <option value="">すべて</option>
            <option value="participant-a">アロナ</option>
            <option value="participant-b">プラナ</option>
            <option value="participant-c">安倍晋三AI</option>
          </select>
        </label>
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
        <div className={styles.loadMore}>
          <button
            className={styles.secondaryButton}
            type="button"
            disabled={records.isFetchingNextPage}
            onClick={() => void records.fetchNextPage()}
          >
            {records.isFetchingNextPage ? "読み込み中" : "さらに読み込む"}
          </button>
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
        <p className={styles.eyebrow}>COMPLETED DEBATE</p>
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
        <p className={styles.eyebrow}>FINAL DECISION</p>
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

function VoteGraph({ record }: { readonly record: RecordDetailResponse }) {
  const positions: Record<ParticipantSlot, number> = {
    "participant-a": 110,
    "participant-b": 360,
    "participant-c": 610,
  };
  return (
    <figure className={styles.voteGraph}>
      <svg viewBox="0 0 720 190" aria-labelledby="vote-graph-title">
        <title id="vote-graph-title">参加者間の投票関係。詳細は直後の一覧に記載しています。</title>
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
            <path d="M0,0 L0,6 L7,3 z" />
          </marker>
        </defs>
        {record.votes.map((vote, index) => (
          <path
            key={vote.voter}
            d={`M ${positions[vote.voter]} 90 Q 360 ${25 + index * 28} ${positions[vote.candidate]} 90`}
            markerEnd="url(#arrow)"
          />
        ))}
        {record.participants.map((person) => (
          <g key={person.slot}>
            <circle cx={positions[person.slot]} cy="110" r="42" />
            <text x={positions[person.slot]} y="116" textAnchor="middle">
              {person.displayName}
            </text>
          </g>
        ))}
      </svg>
      <figcaption>矢印は「誰が誰へ投票したか」を示します。</figcaption>
    </figure>
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
