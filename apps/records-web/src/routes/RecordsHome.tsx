import { useInfiniteQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { RecordsApiError } from "../api/http";
import { getRecords } from "../api/recordList";
import type { ParticipantSlot, SortOrder } from "../api/types";
import { AvatarSelect, type AvatarSelectOption } from "../components/AvatarSelect";
import { DebateCard } from "../components/DebateCard";
import { ErrorPanel } from "../components/ErrorPanel";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import { routeMotionDelay } from "../lib/routePresentation";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/home.module.css";
import routeStyles from "../styles/routeMotion.module.css";

export default function RecordsHome(): React.JSX.Element {
  const [winner, setWinner] = useState<ParticipantSlot | "">("");
  const [sort, setSort] = useState<SortOrder>("newest");
  const [search, setSearch] = useState("");
  const [requester, setRequester] = useState("");
  const loadMoreRef = useRef<HTMLDivElement>(null);
  const observedPageCountRef = useRef<number | undefined>(undefined);
  const [appendMotionIds, setAppendMotionIds] = useState<ReadonlySet<string>>(() => new Set());
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

  useLayoutEffect(() => {
    observedPageCountRef.current = undefined;
    setAppendMotionIds(new Set());
  }, [sort, winner]);

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
      <header
        className={`${commonStyles.pageHeader} ${routeStyles.routeMotionItem}`}
        data-route-motion-ready={records.isPending ? undefined : ""}
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
        data-route-motion-terminal={
          !records.isPending && visibleRecords.length === 0 ? "" : undefined
        }
        style={routeMotionDelay(40)}
        aria-label="記録の絞り込み"
      >
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
                onChange={() => setSort("newest")}
              />
              <span lang="en" aria-hidden="true">
                NEW
              </span>
              <span className={commonStyles.visuallyHidden}>新しい順</span>
            </label>
            <label className={styles.sortOption}>
              <input
                className={commonStyles.visuallyHidden}
                type="radio"
                name="records-sort"
                value="oldest"
                checked={sort === "oldest"}
                onChange={() => setSort("oldest")}
              />
              <span lang="en" aria-hidden="true">
                OLD
              </span>
              <span className={commonStyles.visuallyHidden}>古い順</span>
            </label>
          </div>
        </fieldset>
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
        <section className={commonStyles.emptyState}>
          <span aria-hidden="true">◇</span>
          <h2>該当する記録はありません</h2>
          <p>条件を変更して、もう一度探してみてください。</p>
        </section>
      )}
      <section className={styles.cardGrid} aria-label="完了した議論">
        {visibleRecords.map((record, index) => (
          <DebateCard
            key={record.recordId}
            record={record}
            motionDelay={60 + Math.min(index, 5) * 12}
            motionTerminal={index === visibleRecords.length - 1}
            appended={appendMotionIds.has(record.recordId)}
            onAppendAnimationEnd={consumeAppendMotion}
          />
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
              className={commonStyles.secondaryButton}
              type="button"
              onClick={() => void fetchNextPage()}
            >
              次の記録をもう一度読み込む
            </button>
          ) : localFiltersActive ? (
            <button
              className={commonStyles.secondaryButton}
              type="button"
              onClick={() => void fetchNextPage()}
            >
              検索対象をさらに読み込む
            </button>
          ) : "IntersectionObserver" in window ? (
            <span>この位置までスクロールすると、次の記録を自動で読み込みます。</span>
          ) : (
            <button
              className={commonStyles.secondaryButton}
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
