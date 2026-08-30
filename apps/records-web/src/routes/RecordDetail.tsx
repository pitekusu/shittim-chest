import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { RecordsApiError } from "../api/http";
import { getRecord } from "../api/recordDetail";
import type { ParticipantSlot, RecordDetailResponse } from "../api/types";
import { Avatar } from "../components/Avatar";
import { ErrorPanel } from "../components/ErrorPanel";
import { VoteGraph } from "../components/VoteGraph";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import { formatCompletedDateTime } from "../lib/dateTime";
import { routeMotionDelay } from "../lib/routePresentation";
import commonStyles from "../styles/common.module.css";
import detailStyles from "../styles/detail.module.css";
import routeStyles from "../styles/routeMotion.module.css";
import { NotFoundPage } from "./NotFoundPage";

const RECORD_ID_PATTERN = /^[A-Za-z0-9_-]{43}$/;
const JAPANESE_HEADING_CLASS = `${commonStyles.japaneseText} ${commonStyles.japaneseHeading}`;
const JAPANESE_PROSE_CLASS = `${commonStyles.japaneseText} ${commonStyles.japaneseProse}`;
const READABLE_JAPANESE_PROSE_CLASS = `${JAPANESE_PROSE_CLASS} ${commonStyles.readableMeasure}`;

export default function RecordDetail(): React.JSX.Element {
  const { recordId = "" } = useParams();
  const record = useQuery({
    queryKey: ["record", recordId],
    queryFn: () => getRecord(recordId),
    enabled: RECORD_ID_PATTERN.test(recordId),
  });
  useAuthenticationRecovery(record.error);

  if (!RECORD_ID_PATTERN.test(recordId)) return <NotFoundPage />;
  if (record.isPending) {
    return <p className={routeStyles.routeLoading}>議論の記録を開いています。</p>;
  }
  if (record.isError) {
    const error = record.error instanceof RecordsApiError ? record.error : undefined;
    return (
      <div
        className={routeStyles.routeMotionItem}
        data-route-motion-ready=""
        data-route-motion-terminal=""
      >
        <ErrorPanel
          title="記録を開けませんでした"
          message={error?.message ?? "通信状態を確認してください。"}
          requestId={error?.requestId}
          onRetry={() => void record.refetch()}
        />
      </div>
    );
  }

  return <RecordDocument record={record.data} />;
}

export function RecordDocument({
  record,
}: {
  readonly record: RecordDetailResponse;
}): React.JSX.Element {
  const participant = (slot: ParticipantSlot) =>
    record.participants.find((item) => item.slot === slot)!;
  const count = (slot: ParticipantSlot) =>
    record.result.voteCounts.find((item) => item.participant === slot)?.count ?? 0;

  return (
    <article className={detailStyles.recordDocument} data-route-motion-ready="">
      <header
        className={`${detailStyles.recordHeader} ${commonStyles.recordHeader} ${routeStyles.routeMotionItem}`}
        style={routeMotionDelay(0)}
      >
        <Link className={detailStyles.backLink} to="/">
          ← 記録一覧へ
        </Link>
        <p className={commonStyles.eyebrow} lang="en">
          COMPLETED DEBATE
        </p>
        <h1 className={JAPANESE_HEADING_CLASS} tabIndex={-1}>
          {record.question}
        </h1>
        <div className={detailStyles.recordMeta}>
          <Avatar avatar={record.requester.avatar} />
          <span>
            <small>依頼者</small>
            {record.requester.displayName}
          </span>
          <time dateTime={record.completedAt}>{formatCompletedDateTime(record.completedAt)}</time>
        </div>
      </header>
      <section
        className={`${detailStyles.detailSection} ${routeStyles.routeMotionItem}`}
        style={routeMotionDelay(40)}
        aria-labelledby="opinions-title"
      >
        <h2 id="opinions-title" className={JAPANESE_HEADING_CLASS}>
          3人の意見
        </h2>
        <div className={detailStyles.opinionGrid}>
          {record.participants.map((person) => {
            const initial = record.initialOpinions.find(
              (item) => item.participant === person.slot,
            )!;
            const final = record.finalProposals.find((item) => item.participant === person.slot)!;
            return (
              <article className={detailStyles.opinionCard} key={person.slot}>
                <header>
                  <Avatar avatar={person.avatar} />
                  <h3>{person.displayName}</h3>
                </header>
                <div>
                  <h4>初回意見</h4>
                  <strong className={commonStyles.japaneseText}>{initial.summary}</strong>
                  <p className={JAPANESE_PROSE_CLASS}>{initial.proposal}</p>
                </div>
                <div className={detailStyles.finalProposal}>
                  <h4>最終案</h4>
                  <strong className={commonStyles.japaneseText}>{final.title}</strong>
                  <p className={JAPANESE_PROSE_CLASS}>{final.proposal}</p>
                </div>
              </article>
            );
          })}
        </div>
      </section>
      {record.affection && (
        <section
          className={`${detailStyles.detailSection} ${detailStyles.affectionSection} ${routeStyles.routeMotionItem}`}
          style={routeMotionDelay(80)}
          aria-labelledby="affection-title"
        >
          <header className={detailStyles.affectionHeader}>
            <div>
              <p className={commonStyles.eyebrow} lang="en">
                AFFECTION UPDATE
              </p>
              <h2 id="affection-title" className={JAPANESE_HEADING_CLASS}>
                親愛度の変化
              </h2>
            </div>
            <span className={detailStyles.affectionScale}>0 — 1000</span>
          </header>
          {record.affection.status === "unavailable" && (
            <p className={`${detailStyles.affectionUnavailable} ${JAPANESE_PROSE_CLASS}`}>
              質問の評価を完了できなかったため、親愛度は変更されませんでした。
            </p>
          )}
          <div className={detailStyles.affectionGrid}>
            {record.affection.participants.map((change) => {
              const person = participant(change.participant);
              const signedDelta =
                change.appliedDelta > 0 ? `+${change.appliedDelta}` : String(change.appliedDelta);
              const signedQuestionScore =
                change.questionScore === null
                  ? "未評価"
                  : change.questionScore > 0
                    ? `+${change.questionScore}`
                    : String(change.questionScore);
              return (
                <article
                  className={`${detailStyles.affectionCard} ${detailStyles[change.participant]}`}
                  key={change.participant}
                >
                  <header>
                    <Avatar avatar={person.avatar} />
                    <div>
                      <h3>{person.displayName}</h3>
                      <span>
                        質問評価 <strong>{signedQuestionScore}</strong>
                      </span>
                    </div>
                  </header>
                  <div className={detailStyles.affectionTransition}>
                    <span>
                      <small>変更前</small>
                      <strong>{change.before}</strong>
                    </span>
                    <span
                      className={
                        change.appliedDelta > 0
                          ? detailStyles.affectionPositive
                          : change.appliedDelta < 0
                            ? detailStyles.affectionNegative
                            : detailStyles.affectionNeutral
                      }
                      aria-label={`実増減 ${signedDelta}点`}
                    >
                      {signedDelta}
                    </span>
                    <span>
                      <small>変更後</small>
                      <strong>{change.after}</strong>
                    </span>
                  </div>
                  <meter
                    aria-label={`${person.displayName}の親愛度 ${change.after}点（1000点満点）`}
                    className={detailStyles.affectionMeter}
                    min={0}
                    max={1000}
                    value={change.after}
                  />
                </article>
              );
            })}
          </div>
        </section>
      )}
      <section
        className={`${detailStyles.detailSection} ${routeStyles.routeMotionItem}`}
        style={routeMotionDelay(record.affection ? 120 : 80)}
        aria-labelledby="votes-title"
      >
        <h2 id="votes-title" className={JAPANESE_HEADING_CLASS}>
          投票
        </h2>
        <VoteGraph record={record} />
        <div className={detailStyles.voteList}>
          {record.votes.map((vote) => (
            <article key={vote.voter} className={detailStyles.voteCard}>
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
          <p className={`${detailStyles.tieNotice} ${JAPANESE_PROSE_CLASS}`}>
            同票のため、シッテムの箱の既定ルールで勝者を決定しました。
          </p>
        )}
      </section>
      <section
        className={`${detailStyles.detailSection} ${detailStyles.decisionSection} ${routeStyles.routeMotionItem}`}
        data-route-motion-terminal=""
        style={routeMotionDelay(record.affection ? 160 : 120)}
        aria-labelledby="decision-title"
      >
        <p className={commonStyles.eyebrow} lang="en">
          FINAL DECISION
        </p>
        <h2 id="decision-title" className={JAPANESE_HEADING_CLASS}>
          最終決定
        </h2>
        <div className={detailStyles.winnerPanel}>
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
        <p className={`${detailStyles.decisionText} ${READABLE_JAPANESE_PROSE_CLASS}`}>
          {record.finalDecision.decision}
        </p>
        <div className={detailStyles.decisionColumns}>
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
