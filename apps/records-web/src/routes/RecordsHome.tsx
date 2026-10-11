import { useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { RecordsApiError } from "../api/http";
import { getRecordRequesters, getRecords, searchRecords } from "../api/recordList";
import type { ParticipantSlot, RequesterSummary } from "../api/types";
import { AvatarSelect, type AvatarSelectOption } from "../components/AvatarSelect";
import { DebateCard } from "../components/DebateCard";
import { ErrorPanel } from "../components/ErrorPanel";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import { useRecordsArchive } from "../hooks/useRecordsArchive";
import { routeMotionDelay } from "../lib/routePresentation";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/home.module.css";
import routeStyles from "../styles/routeMotion.module.css";

export default function RecordsHome({
  csrfToken,
}: {
  readonly csrfToken: string;
}): React.JSX.Element {
  const client = useQueryClient();
  const { filters, requestedCount, showMore, updateFilters, rememberRecord } = useRecordsArchive();
  const { winner, sort, search, requester } = filters;
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const observedPageCountRef = useRef<number | undefined>(undefined);
  const [appendMotionIds, setAppendMotionIds] = useState<ReadonlySet<string>>(() => new Set());
  const [composing, setComposing] = useState(false);
  const normalizedSearch = search.trim();
  const [debouncedSearch, setDebouncedSearch] = useState(normalizedSearch);
  useEffect(() => {
    if (composing) return;
    if (!normalizedSearch) {
      setDebouncedSearch("");
      return;
    }
    const timeout = window.setTimeout(() => setDebouncedSearch(normalizedSearch), 300);
    return () => window.clearTimeout(timeout);
  }, [composing, normalizedSearch]);
  const criteriaReady = !composing && normalizedSearch === debouncedSearch;
  const searchActive = debouncedSearch !== "" || requester !== "";
  const queryKey = useMemo(
    () => ["records", searchActive ? "search" : "list", winner, sort, debouncedSearch, requester],
    [searchActive, winner, sort, debouncedSearch, requester],
  );
  const records = useInfiniteQuery({
    queryKey,
    enabled: criteriaReady,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => {
      const filters = { cursor: pageParam, sort, winner: winner || undefined };
      return searchActive
        ? searchRecords(
            { ...filters, search: debouncedSearch, requester: requester || undefined },
            csrfToken,
            signal,
          )
        : getRecords(filters, signal);
    },
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  });
  const candidates = useInfiniteQuery({
    queryKey: ["records", "requesters"],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => getRecordRequesters(pageParam, signal),
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  });
  const { fetchNextPage, hasNextPage, isFetchingNextPage, isFetchNextPageError } = records;
  useAuthenticationRecovery(records.error);
  useAuthenticationRecovery(candidates.error);

  useEffect(() => {
    if (!criteriaReady) void client.cancelQueries({ queryKey, exact: true });
  }, [client, criteriaReady, queryKey]);

  useEffect(() => {
    if (candidates.hasNextPage && !candidates.isFetching && !candidates.isError) {
      void candidates.fetchNextPage();
    }
  }, [candidates]);

  useEffect(() => {
    if (
      records.error instanceof RecordsApiError &&
      records.error.code === "CURSOR_INVALID" &&
      records.data !== undefined &&
      criteriaReady
    ) {
      void client.resetQueries({ queryKey, exact: true });
    }
    if (
      candidates.error instanceof RecordsApiError &&
      candidates.error.code === "CURSOR_INVALID" &&
      candidates.data !== undefined
    ) {
      void client.resetQueries({ queryKey: ["records", "requesters"], exact: true });
    }
  }, [
    candidates.data,
    candidates.error,
    client,
    criteriaReady,
    queryKey,
    records.data,
    records.error,
  ]);

  const loadedRecords = useMemo(
    () => records.data?.pages.flatMap((page) => page.items) ?? [],
    [records.data],
  );

  useLayoutEffect(() => {
    observedPageCountRef.current = undefined;
    setAppendMotionIds(new Set());
  }, [sort, winner, debouncedSearch, requester]);

  useLayoutEffect(() => {
    const pages = records.data?.pages;
    if (!pages) return;
    const previousPageCount = observedPageCountRef.current;
    observedPageCountRef.current = pages.length;
    if (previousPageCount === undefined) return;
    if (pages.length < previousPageCount) {
      setAppendMotionIds(new Set());
      return;
    }
    if (pages.length === previousPageCount) return;
    const addedRecordIds = pages
      .slice(previousPageCount)
      .flatMap((page) => page.items.map((record) => record.recordId));
    if (addedRecordIds.length === 0) return;
    setAppendMotionIds((current) => {
      const next = new Set(current);
      for (const recordId of addedRecordIds) next.add(recordId);
      return next;
    });
  }, [records.data?.pages]);

  const consumeAppendMotion = useCallback((recordId: string) => {
    setAppendMotionIds((current) => {
      if (!current.has(recordId)) return current;
      const next = new Set(current);
      next.delete(recordId);
      return next;
    });
  }, []);

  const requesterOptions = useMemo<readonly AvatarSelectOption<string>[]>(() => {
    const requesters = new Map<string, RequesterSummary>();
    for (const page of candidates.data?.pages ?? []) {
      for (const item of page.items) {
        if (!requesters.has(item.displayName)) requesters.set(item.displayName, item);
      }
    }
    // A chosen name remains available while candidates refresh or return no records.
    if (requester && !requesters.has(requester)) {
      requesters.set(requester, {
        displayName: requester,
        avatar: {
          kind: "placeholder",
          url: null,
          alt: `${requester}のアバター`,
          fallbackVariant: "cyan",
        },
      });
    }
    return [
      { value: "", label: "すべて", avatar: null },
      ...Array.from(requesters.values())
        .sort((a, b) => a.displayName.localeCompare(b.displayName, "ja-JP"))
        .map((item) => ({ value: item.displayName, label: item.displayName, avatar: item.avatar })),
    ];
  }, [candidates.data, requester]);

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

  const visibleRecords = useMemo(
    () => (criteriaReady ? loadedRecords.slice(0, requestedCount) : []),
    [criteriaReady, loadedRecords, requestedCount],
  );
  const needsMore =
    (searchActive || requestedCount > 12) && loadedRecords.length < requestedCount && hasNextPage;
  const searching = !criteriaReady || records.isPending || (needsMore && !records.isError);
  const canShowMore = criteriaReady && (hasNextPage || loadedRecords.length > requestedCount);

  useEffect(() => {
    if (criteriaReady && needsMore && !records.isFetching && !records.isError) {
      void fetchNextPage();
    }
  }, [criteriaReady, fetchNextPage, needsMore, records.isError, records.isFetching]);

  useLayoutEffect(() => {
    const visibleRecordIds = new Set(visibleRecords.map((record) => record.recordId));
    setAppendMotionIds((current) => {
      if ([...current].every((recordId) => visibleRecordIds.has(recordId))) return current;
      return new Set([...current].filter((recordId) => visibleRecordIds.has(recordId)));
    });
  }, [visibleRecords]);

  useEffect(() => {
    const sentinel = loadMoreRef.current;
    if (
      !sentinel ||
      !canShowMore ||
      searching ||
      records.isFetching ||
      records.isError ||
      !("IntersectionObserver" in window)
    )
      return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (!entry?.isIntersecting) return;
        observer.unobserve(sentinel);
        showMore();
      },
      { rootMargin: "320px 0px", threshold: 0 },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [canShowMore, records.isError, records.isFetching, requestedCount, searching, showMore]);

  const error = records.error instanceof RecordsApiError ? records.error : undefined;

  return (
    <div className={styles.archivePage}>
      <header
        className={`${commonStyles.pageHeader} ${routeStyles.routeMotionItem}`}
        data-route-motion-ready={searching ? undefined : ""}
        style={routeMotionDelay(0)}
      >
        <p className={commonStyles.eyebrow} lang="en">
          RECORDS ARCHIVE
        </p>
        <h1
          className={`${commonStyles.japaneseText} ${commonStyles.japaneseHeading}`}
          tabIndex={-1}
        >
          議論の記録
        </h1>
      </header>
      <section
        className={`${styles.filters} ${routeStyles.routeMotionItem}`}
        data-route-motion-terminal={!searching && visibleRecords.length === 0 ? "" : undefined}
        style={routeMotionDelay(40)}
        aria-label="記録の絞り込み"
      >
        <label>
          フリーワード検索
          <input
            type="search"
            value={search}
            onChange={(event) => updateFilters({ search: event.target.value })}
            onCompositionStart={() => setComposing(true)}
            onCompositionEnd={(event) => {
              updateFilters({ search: event.currentTarget.value });
              setComposing(false);
            }}
            maxLength={1000}
            placeholder="質問文などを入力"
            aria-describedby="archive-search-scope"
          />
        </label>
        <AvatarSelect
          label="依頼者"
          value={requester}
          options={requesterOptions}
          onChange={(value) => updateFilters({ requester: value })}
        />
        <AvatarSelect
          label="勝者"
          value={winner}
          options={winnerOptions}
          onChange={(value) => updateFilters({ winner: value })}
        />
        <fieldset className={styles.sortField}>
          <legend>並び順</legend>
          <div className={styles.sortSegment} data-sort={sort}>
            <label className={styles.sortOption}>
              <input
                className={commonStyles.visuallyHidden}
                type="radio"
                name="records-sort"
                value="newest"
                checked={sort === "newest"}
                onChange={() => updateFilters({ sort: "newest" })}
              />
              <span>新しい順</span>
            </label>
            <label className={styles.sortOption}>
              <input
                className={commonStyles.visuallyHidden}
                type="radio"
                name="records-sort"
                value="oldest"
                checked={sort === "oldest"}
                onChange={() => updateFilters({ sort: "oldest" })}
              />
              <span>古い順</span>
            </label>
          </div>
        </fieldset>
      </section>
      {candidates.isFetching && (
        <p className={styles.loadingLine} aria-live="polite">
          依頼者の候補を読み込んでいます。
        </p>
      )}
      {candidates.isError && (
        <ErrorPanel
          title="依頼者の候補を読み込めませんでした"
          message="通信状態を確認してください。"
          onRetry={() =>
            void (candidates.isFetchNextPageError
              ? candidates.fetchNextPage()
              : candidates.refetch())
          }
        />
      )}
      <div className={styles.archiveResults}>
        <p id="archive-search-scope" className={styles.searchScope} aria-live="polite">
          {normalizedSearch || requester ? "全記録の質問文・勝者名を検索" : "完了した議論を表示"}
          {criteriaReady && visibleRecords.length > 0 && (
            <>
              {" "}
              · <strong>{visibleRecords.length}</strong> 件を表示
            </>
          )}
          {searching ? " · 検索中" : !hasNextPage && !records.isError ? " · すべて確認済み" : ""}
        </p>
        {(normalizedSearch !== "" || requester !== "" || winner !== "") && (
          <button
            className={styles.clearFilters}
            type="button"
            onClick={() => updateFilters({ search: "", requester: "", winner: "" })}
          >
            絞り込みを解除
          </button>
        )}
      </div>
      {searching && (
        <p className={styles.loadingLine} aria-live="polite">
          {normalizedSearch || requester ? "全記録から探しています。" : "記録を読み込んでいます。"}
        </p>
      )}
      {criteriaReady && records.isError && (
        <ErrorPanel
          title="記録を読み込めませんでした"
          message={error?.message ?? "通信状態を確認してください。"}
          requestId={error?.requestId}
          onRetry={() => void (isFetchNextPageError ? fetchNextPage() : records.refetch())}
        />
      )}
      {!searching && !records.isError && !hasNextPage && visibleRecords.length === 0 && (
        <section className={commonStyles.emptyState}>
          <span aria-hidden="true">◇</span>
          <h2>該当する記録はありません</h2>
          <p>条件を変更して、もう一度探してみてください。</p>
        </section>
      )}
      <section
        className={styles.cardGrid}
        aria-label="完了した議論"
        data-archive-ready={!searching && !records.isFetching ? "" : undefined}
      >
        {visibleRecords.map((record, index) => (
          <DebateCard
            key={record.recordId}
            record={record}
            motionDelay={60 + Math.min(index, 5) * 12}
            motionTerminal={index === visibleRecords.length - 1}
            appended={appendMotionIds.has(record.recordId)}
            onAppendAnimationEnd={consumeAppendMotion}
            onOpen={rememberRecord}
          />
        ))}
      </section>
      {canShowMore && (
        <div
          className={styles.loadMore}
          ref={loadMoreRef}
          aria-busy={isFetchingNextPage}
          aria-live="polite"
        >
          {searching || isFetchingNextPage ? (
            <span>次の記録を読み込んでいます。</span>
          ) : isFetchNextPageError ? (
            <button
              className={commonStyles.secondaryButton}
              type="button"
              onClick={() => void fetchNextPage()}
            >
              次の記録をもう一度読み込む
            </button>
          ) : "IntersectionObserver" in window ? (
            <span>この位置までスクロールすると、次の記録を自動で読み込みます。</span>
          ) : (
            <button className={commonStyles.secondaryButton} type="button" onClick={showMore}>
              さらに読み込む
            </button>
          )}
        </div>
      )}
    </div>
  );
}
