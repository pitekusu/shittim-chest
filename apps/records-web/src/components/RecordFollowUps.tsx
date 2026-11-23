import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { RecordsApiError } from "../api/http";
import { getRecordFollowUps } from "../api/recordDetail";
import type { RecordDetailResponse } from "../api/types";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import { formatCompletedDateTime } from "../lib/dateTime";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/detail.module.css";
import { ErrorPanel } from "./ErrorPanel";

export function RecordFollowUps({ record }: { readonly record: RecordDetailResponse }) {
  const branches = useInfiniteQuery({
    queryKey: ["record-follow-ups", record.recordId],
    queryFn: ({ pageParam, signal }) => getRecordFollowUps(record.recordId, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.nextCursor ?? undefined,
  });
  useAuthenticationRecovery(branches.error);
  const error = branches.error instanceof RecordsApiError ? branches.error : undefined;
  const followUps = branches.data?.pages.flatMap(({ items }) => items) ?? [];

  return (
    <section className={styles.detailSection} aria-labelledby="followups">
      <h2
        id="followups"
        className={`${commonStyles.japaneseText} ${commonStyles.japaneseHeading}`}
        tabIndex={-1}
      >
        更問
      </h2>
      <ol className={styles.branchList} aria-label="議論のつながり">
        {record.parentRecordId && (
          <li>
            <Link className={styles.branchLink} to={`/records/${record.parentRecordId}`}>
              <span>元の議論へ</span>
              {record.parentQuestionPreview && (
                <strong className={commonStyles.japaneseText}>
                  {record.parentQuestionPreview}
                </strong>
              )}
            </Link>
          </li>
        )}
        <li aria-current="true">
          <div className={styles.branchNode}>
            <span>この議論</span>
            <strong className={commonStyles.japaneseText}>{record.question}</strong>
            <time dateTime={record.completedAt}>{formatCompletedDateTime(record.completedAt)}</time>
          </div>
        </li>
        {followUps.map((followUp) => (
          <li key={followUp.recordId} className={styles.branchChild}>
            <Link className={styles.branchLink} to={`/records/${followUp.recordId}`}>
              <strong className={commonStyles.japaneseText}>{followUp.questionPreview}</strong>
              <time dateTime={followUp.completedAt}>
                {formatCompletedDateTime(followUp.completedAt)}
              </time>
            </Link>
          </li>
        ))}
      </ol>
      {branches.isPending && <output>更問を読み込んでいます。</output>}
      {branches.isSuccess && followUps.length === 0 && <p>完了した更問はありません。</p>}
      {branches.isError && (
        <ErrorPanel
          title="更問を読み込めませんでした"
          message={error?.message ?? "通信状態を確認してください。"}
          requestId={error?.requestId}
          onRetry={() =>
            void (branches.isFetchNextPageError ? branches.fetchNextPage() : branches.refetch())
          }
        />
      )}
      {branches.hasNextPage && !branches.isFetchNextPageError && (
        <button
          className={commonStyles.secondaryButton}
          type="button"
          aria-disabled={branches.isFetchingNextPage}
          onClick={() => {
            if (!branches.isFetchingNextPage) void branches.fetchNextPage();
          }}
        >
          {branches.isFetchingNextPage ? "更問を読み込んでいます。" : "更問をさらに読み込む"}
        </button>
      )}
    </section>
  );
}
