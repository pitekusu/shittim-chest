import { useEffect, useState, type JSX } from "react";

import { RecordsApiError } from "../api/http";
import type { AdminPromptKey } from "../api/types";
import {
  PROMPT_KEYS,
  PROMPT_LIMIT_BYTES,
  SYSTEM_CONFIRMATION,
  isRevisionConflict,
  normalizePrompt,
  promptBytes,
  promptIsValid,
  useAdminPromptEditor,
} from "../hooks/useAdminPromptEditor";
import { ADMIN_PROMPT_APPLICATION_LABELS } from "../lib/adminPromptState";
import { formatCompletedDateTime } from "../lib/dateTime";
import { lineDiff } from "../lib/lineDiff";
import adminStyles from "../styles/admin.module.css";
import commonStyles from "../styles/common.module.css";
import { AdminPanelState as PanelState } from "./AdminPanelState";

const PROMPT_PRESENTATION: Readonly<
  Record<AdminPromptKey, { readonly label: string; readonly description: string }>
> = {
  system: {
    label: "システム",
    description:
      "全OpenAI requestへ共通で加える基本方針です。コード所有の安全制約は変更されません。",
  },
  moderator: {
    label: "事前調査AI",
    description: "検索の要否を判断し、参加者へ渡すEvidenceを準備するモデレータの指示です。",
  },
  participantA: { label: "アロナ", description: "アロナの話し方と判断軸を定める人格指示です。" },
  participantB: { label: "プラナ", description: "プラナの話し方と判断軸を定める人格指示です。" },
  participantC: {
    label: "安倍晋三AI",
    description: "安倍晋三AIの話し方と判断軸を定める人格指示です。",
  },
};

interface AdminPromptManagerProps {
  readonly canWrite: boolean;
  readonly csrfToken: string;
}

function promptTabTarget(current: AdminPromptKey, key: string): AdminPromptKey | null {
  const currentIndex = PROMPT_KEYS.indexOf(current);
  if (key === "Home") return PROMPT_KEYS[0] ?? null;
  if (key === "End") return PROMPT_KEYS.at(-1) ?? null;
  if (key === "ArrowRight" || key === "ArrowDown") {
    return PROMPT_KEYS[(currentIndex + 1) % PROMPT_KEYS.length] ?? null;
  }
  if (key === "ArrowLeft" || key === "ArrowUp") {
    return PROMPT_KEYS[(currentIndex - 1 + PROMPT_KEYS.length) % PROMPT_KEYS.length] ?? null;
  }
  return null;
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof RecordsApiError ? error.message : fallback;
}

function SystemConfirmation({
  before,
  after,
  value,
  onChange,
  labelPrefix,
}: {
  readonly before: string;
  readonly after: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly labelPrefix: string;
}): JSX.Element {
  return (
    <div className={adminStyles.promptSystemWarning} role="alert">
      <strong>システムプロンプトが変更されます。</strong>
      <span>コード側の安全境界は編集対象外です。変更前後を確認してください。</span>
      <div className={adminStyles.promptDiff}>
        <section>
          <h3>変更前</h3>
          <pre>{before}</pre>
        </section>
        <section>
          <h3>変更後</h3>
          <pre>{after}</pre>
        </section>
      </div>
      <label className={adminStyles.promptConfirmationField}>
        {labelPrefix}確認文字列: <code>{SYSTEM_CONFIRMATION}</code>
        <input
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      </label>
    </div>
  );
}

function PromptLineDiff({ before, after }: { readonly before: string; readonly after: string }) {
  const entries = lineDiff(before, after);
  const changed = entries.some((entry) => entry.kind !== "context");
  if (!changed) return <p className={adminStyles.promptDiffUnchanged}>現在の内容と同一です。</p>;

  return (
    <div className={adminStyles.promptUnifiedDiff}>
      <div className={adminStyles.promptDiffLegend} aria-hidden="true">
        <span data-kind="removed">− 選択revision</span>
        <span data-kind="added">＋ 現在</span>
      </div>
      <div className={adminStyles.promptDiffRows}>
        <table className={adminStyles.promptDiffTable} aria-label="選択revisionから現在への行差分">
          <tbody>
            {entries.map((entry, index) => (
              <tr className={adminStyles.promptDiffLine} data-kind={entry.kind} key={index}>
                <td
                  aria-label={
                    entry.beforeLine === null
                      ? "選択revisionの行なし"
                      : `選択revision ${entry.beforeLine}行目`
                  }
                >
                  {entry.beforeLine ?? ""}
                </td>
                <td
                  aria-label={
                    entry.afterLine === null ? "現在の行なし" : `現在 ${entry.afterLine}行目`
                  }
                >
                  {entry.afterLine ?? ""}
                </td>
                <td
                  aria-label={
                    entry.kind === "removed" ? "削除" : entry.kind === "added" ? "追加" : "変更なし"
                  }
                >
                  {entry.kind === "removed" ? "−" : entry.kind === "added" ? "+" : " "}
                </td>
                <td>
                  <code>{entry.text.length === 0 ? " " : entry.text}</code>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function AdminPromptManager({
  canWrite,
  csrfToken,
}: AdminPromptManagerProps): JSX.Element {
  const [selectedPrompt, setSelectedPrompt] = useState<AdminPromptKey>("system");
  const {
    prompts,
    status,
    revisions,
    revision,
    baseSnapshot,
    drafts,
    confirmation,
    setConfirmation,
    selectedRevision,
    selectRevision,
    rollbackTarget,
    rollbackConfirmation,
    setRollbackConfirmation,
    successMessage,
    latestConflict,
    synchronization,
    syncError,
    syncPending,
    retrySynchronization,
    applyMutation,
    rollbackMutation,
    systemChanged,
    invalidPrompts,
    canApply,
    canRollback,
    rollbackSameContent,
    rollbackSystemChanged,
    isLegacyRegistration,
    allRevisions,
    selectedIsCurrent,
    applicationState,
    updateDraft,
    adoptLatestRevision,
    startRollback,
    cancelRollback,
    closeRevision,
  } = useAdminPromptEditor(canWrite, csrfToken);

  useEffect(() => {
    if (selectedRevision !== null) {
      document.getElementById("prompt-revision-detail-title")?.focus();
    }
  }, [selectedRevision]);

  return (
    <div className={adminStyles.promptWorkspace}>
      <section
        className={`${adminStyles.adminPanel} ${adminStyles.promptOverview}`}
        aria-labelledby="prompt-overview-title"
      >
        <header className={adminStyles.panelHeader}>
          <div>
            <p className={adminStyles.panelEyebrow} lang="en">
              ACTIVE CONFIGURATION
            </p>
            <h2 id="prompt-overview-title">現在の設定</h2>
          </div>
          <span
            className={adminStyles.stateBadge}
            data-tone={applicationState === "applied" ? undefined : "warning"}
          >
            <span className={adminStyles.stateDot} aria-hidden="true" />
            {ADMIN_PROMPT_APPLICATION_LABELS[applicationState]}
          </span>
        </header>
        <dl className={adminStyles.promptOverviewFacts}>
          <div>
            <dt>設定方式</dt>
            <dd>{prompts.data?.mode === "managed" ? "管理版" : "既存設定"}</dd>
          </div>
          <div>
            <dt>有効revision</dt>
            <dd className={adminStyles.promptRevision}>
              {prompts.data?.activeRevision ?? "未作成"}
            </dd>
          </div>
          <div>
            <dt>作成日時</dt>
            <dd>
              {prompts.data?.createdAt ? formatCompletedDateTime(prompts.data.createdAt) : "未作成"}
            </dd>
          </div>
        </dl>
        {status.isError && (
          <p className={adminStyles.promptStatusNote}>
            AWS状態を確認できないため、反映状態は「保存済み」として表示しています。
            {canWrite ? "編集" : "閲覧"}は継続できます。
          </p>
        )}
      </section>

      <section
        className={`${adminStyles.adminPanel} ${adminStyles.promptEditorPanel}`}
        aria-labelledby="prompt-editor-title"
      >
        <header className={adminStyles.panelHeader}>
          <div>
            <p className={adminStyles.panelEyebrow} lang="en">
              {canWrite ? "PROMPT EDITOR" : "PROMPT VIEWER"}
            </p>
            <h2 id="prompt-editor-title">{canWrite ? "プロンプト編集" : "プロンプト参照"}</h2>
          </div>
        </header>
        {!canWrite && (
          <p className={adminStyles.promptStatusNote}>
            閲覧専用です。プロンプトの反映とrevisionの復元は管理者だけが実行できます。
          </p>
        )}
        {prompts.isPending && (
          <PanelState
            busy
            title="プロンプトを読み込んでいます"
            message="設定本文はこの画面だけで扱います。"
          />
        )}
        {prompts.isError && syncError === null && (
          <PanelState
            title={
              baseSnapshot === null
                ? "プロンプトを読み込めませんでした"
                : "設定を更新できませんでした"
            }
            message={errorMessage(prompts.error, "通信状態を確認してください。")}
            onRetry={() => void prompts.refetch()}
          />
        )}
        {syncError !== null && synchronization !== null && (
          <PanelState
            busy={syncPending}
            title={
              synchronization.kind === "saved"
                ? "保存した設定を再取得できませんでした"
                : "最新revisionを取得できませんでした"
            }
            message={
              synchronization.kind === "saved"
                ? "保存は完了しています。入力内容を保持したまま、最新の設定を再取得してください。"
                : "入力内容と使用中のrevisionを保持しています。最新revisionの取得後に基準を更新できます。"
            }
            onRetry={retrySynchronization}
          />
        )}
        {baseSnapshot !== null && drafts !== null && (
          <div className={adminStyles.promptEditorBody}>
            {canWrite && latestConflict !== null && (
              <div className={adminStyles.promptConflict} role="alert">
                <div>
                  <strong>別の画面で新しいrevisionが保存されました。</strong>
                  <span>
                    入力中の内容は保持しています。比較後、明示的に基準だけを更新してください。
                  </span>
                  <small>
                    使用中: {baseSnapshot.activeRevision ?? "既存設定"} ／ 最新:{" "}
                    {latestConflict.activeRevision ?? "既存設定"}
                  </small>
                </div>
                <button
                  className={commonStyles.secondaryButton}
                  type="button"
                  onClick={adoptLatestRevision}
                >
                  最新revisionを基準にする
                </button>
              </div>
            )}
            <div
              className={adminStyles.promptTabs}
              role="tablist"
              aria-label={canWrite ? "編集するプロンプト" : "参照するプロンプト"}
            >
              {PROMPT_KEYS.map((key) => (
                <button
                  aria-controls="admin-prompt-editor"
                  aria-selected={selectedPrompt === key}
                  id={`admin-prompt-tab-${key}`}
                  key={key}
                  role="tab"
                  tabIndex={selectedPrompt === key ? 0 : -1}
                  type="button"
                  onClick={() => setSelectedPrompt(key)}
                  onKeyDown={(event) => {
                    const target = promptTabTarget(key, event.key);
                    if (target === null) return;
                    event.preventDefault();
                    setSelectedPrompt(target);
                    document.querySelector<HTMLElement>(`#admin-prompt-tab-${target}`)?.focus();
                  }}
                >
                  {PROMPT_PRESENTATION[key].label}
                </button>
              ))}
            </div>
            <div
              className={adminStyles.promptEditor}
              id="admin-prompt-editor"
              role="tabpanel"
              aria-labelledby={`admin-prompt-tab-${selectedPrompt}`}
            >
              <div className={adminStyles.promptEditorHeading}>
                <label htmlFor={`admin-prompt-${selectedPrompt}`}>
                  {PROMPT_PRESENTATION[selectedPrompt].label}プロンプト
                </label>
                <span
                  className={adminStyles.promptByteCount}
                  data-invalid={!promptIsValid(drafts[selectedPrompt]) || undefined}
                >
                  {promptBytes(drafts[selectedPrompt]).toLocaleString("ja-JP")} /{" "}
                  {PROMPT_LIMIT_BYTES.toLocaleString("ja-JP")} bytes
                </span>
              </div>
              <p>{PROMPT_PRESENTATION[selectedPrompt].description}</p>
              <textarea
                id={`admin-prompt-${selectedPrompt}`}
                readOnly={!canWrite}
                spellCheck={false}
                value={drafts[selectedPrompt]}
                onChange={(event) => updateDraft(selectedPrompt, event.target.value)}
              />
              {canWrite && systemChanged && (
                <SystemConfirmation
                  before={baseSnapshot.prompts.system}
                  after={drafts.system}
                  value={confirmation}
                  onChange={setConfirmation}
                  labelPrefix="変更用"
                />
              )}
              {canWrite && (
                <div className={adminStyles.promptActions}>
                  <p aria-live="polite">
                    {invalidPrompts.length > 0
                      ? "空のプロンプト、または3,500 bytesを超える内容は保存できません。"
                      : (successMessage ?? "未保存の変更はこのブラウザ内だけにあります。")}
                  </p>
                  <button
                    className={commonStyles.primaryButton}
                    type="button"
                    disabled={!canApply}
                    onClick={() => applyMutation.mutate()}
                  >
                    {applyMutation.isPending
                      ? "保存しています"
                      : isLegacyRegistration
                        ? "現在の設定を管理版として登録"
                        : "変更を反映"}
                  </button>
                </div>
              )}
              {canWrite && applyMutation.isError && (
                <PanelState
                  title={
                    isRevisionConflict(applyMutation.error)
                      ? "revisionが競合しました"
                      : "変更を保存できませんでした"
                  }
                  message={errorMessage(
                    applyMutation.error,
                    "入力内容を保持したまま、もう一度お試しください。",
                  )}
                />
              )}
            </div>
          </div>
        )}
      </section>

      <section
        className={`${adminStyles.adminPanel} ${adminStyles.promptHistory}`}
        data-route-motion-terminal=""
        aria-labelledby="prompt-history-title"
      >
        <header className={adminStyles.panelHeader}>
          <div>
            <p className={adminStyles.panelEyebrow} lang="en">
              REVISION HISTORY
            </p>
            <h2 id="prompt-history-title">変更履歴</h2>
          </div>
        </header>
        {revisions.isPending && (
          <PanelState
            busy
            title="履歴を読み込んでいます"
            message="本文は選択するまで取得しません。"
          />
        )}
        {revisions.isError && (
          <PanelState
            title="履歴を読み込めませんでした"
            message={errorMessage(revisions.error, "通信状態を確認してください。")}
            onRetry={() => void revisions.refetch()}
          />
        )}
        {!revisions.isPending && !revisions.isError && allRevisions.length === 0 && (
          <p className={adminStyles.promptEmpty}>管理版revisionはまだありません。</p>
        )}
        {allRevisions.length > 0 && (
          <ul className={adminStyles.promptHistoryList}>
            {allRevisions.map((item) => {
              const isCurrent = item.revision === prompts.data?.activeRevision;
              return (
                <li key={item.revision} data-current={isCurrent || undefined}>
                  <div className={adminStyles.promptHistoryMetadata}>
                    <strong className={adminStyles.promptRevision}>{item.revision}</strong>
                    <span>
                      {item.action === "rollback" ? "復元" : "更新"}・
                      {formatCompletedDateTime(item.createdAt)}
                    </span>
                    <small>
                      元revision: {item.sourceRevision ?? item.baseRevision ?? "既存設定"}
                    </small>
                    <small className={adminStyles.promptChecksum}>
                      checksum {item.checksum.slice(0, 12)}
                    </small>
                  </div>
                  <div className={adminStyles.promptHistoryActions}>
                    {isCurrent ? (
                      <span
                        className={`${commonStyles.secondaryButton} ${adminStyles.promptHistoryCompactAction} ${adminStyles.promptCurrentBadge}`}
                      >
                        使用中
                      </span>
                    ) : (
                      <>
                        <button
                          className={commonStyles.secondaryButton}
                          id={`prompt-revision-trigger-${item.revision}`}
                          type="button"
                          aria-pressed={selectedRevision === item.revision}
                          onClick={() => selectRevision(item.revision)}
                        >
                          変更点を見る
                        </button>
                        {canWrite && (
                          <button
                            className={`${commonStyles.secondaryButton} ${adminStyles.promptHistoryCompactAction}`}
                            type="button"
                            onClick={() => startRollback(item.revision)}
                          >
                            復元
                          </button>
                        )}
                      </>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
        {revisions.hasNextPage && (
          <button
            className={`${commonStyles.secondaryButton} ${adminStyles.promptLoadMore}`}
            type="button"
            disabled={revisions.isFetchingNextPage}
            onClick={() => void revisions.fetchNextPage()}
          >
            {revisions.isFetchingNextPage ? "読み込んでいます" : "さらに履歴を読み込む"}
          </button>
        )}
      </section>
      {selectedRevision !== null && (
        <section
          className={`${adminStyles.adminPanel} ${adminStyles.promptRevisionDetail}`}
          data-route-motion-terminal=""
          aria-labelledby="prompt-revision-detail-title"
        >
          <header>
            <div>
              <h2 id="prompt-revision-detail-title" tabIndex={-1}>
                revisionを比較
              </h2>
              <p className={adminStyles.promptRevision}>{selectedRevision}</p>
            </div>
            <button
              className={commonStyles.secondaryButton}
              type="button"
              onClick={() => {
                document.getElementById(`prompt-revision-trigger-${selectedRevision}`)?.focus();
                closeRevision();
              }}
            >
              閉じる
            </button>
          </header>
          {revision.isPending && (
            <PanelState
              busy
              title="revision本文を読み込んでいます"
              message="選択した版だけを取得しています。"
            />
          )}
          {revision.isError && (
            <PanelState
              title="revisionを読み込めませんでした"
              message={errorMessage(revision.error, "通信状態を確認してください。")}
              onRetry={() => void revision.refetch()}
            />
          )}
          {revision.data !== undefined && prompts.data !== undefined && (
            <>
              <div className={adminStyles.promptRevisionCompare}>
                {PROMPT_KEYS.map((key) => (
                  <details
                    key={key}
                    open={key === "system"}
                    data-changed={
                      normalizePrompt(prompts.data.prompts[key]) !==
                        normalizePrompt(revision.data.prompts[key]) || undefined
                    }
                  >
                    <summary>{PROMPT_PRESENTATION[key].label}</summary>
                    <PromptLineDiff
                      before={revision.data.prompts[key]}
                      after={prompts.data.prompts[key]}
                    />
                  </details>
                ))}
              </div>
              {canWrite && rollbackTarget === revision.data.revision && (
                <div className={adminStyles.promptRollbackConfirmation}>
                  <h3>新しいrevisionとして復元します</h3>
                  <p>
                    選択した本文からimmutable
                    revisionを新規作成します。過去のpointerへ直接戻す操作ではありません。
                  </p>
                  {rollbackSameContent && (
                    <p role="alert">現在の内容と同一のため復元できません。</p>
                  )}
                  {rollbackSystemChanged && (
                    <SystemConfirmation
                      before={prompts.data.prompts.system}
                      after={revision.data.prompts.system}
                      value={rollbackConfirmation}
                      onChange={setRollbackConfirmation}
                      labelPrefix="復元用"
                    />
                  )}
                  <div className={adminStyles.promptHistoryActions}>
                    <button
                      className={commonStyles.secondaryButton}
                      type="button"
                      onClick={cancelRollback}
                    >
                      キャンセル
                    </button>
                    <button
                      className={commonStyles.primaryButton}
                      type="button"
                      disabled={!canRollback}
                      onClick={() => rollbackMutation.mutate()}
                    >
                      {rollbackMutation.isPending ? "復元しています" : "新しい版として復元"}
                    </button>
                  </div>
                  {rollbackMutation.isError && (
                    <PanelState
                      title={
                        isRevisionConflict(rollbackMutation.error)
                          ? "revisionが競合しました"
                          : "復元できませんでした"
                      }
                      message={errorMessage(
                        rollbackMutation.error,
                        "選択内容を保持したまま、もう一度お試しください。",
                      )}
                    />
                  )}
                </div>
              )}
              {canWrite &&
                !selectedIsCurrent &&
                rollbackTarget === null &&
                !rollbackSameContent && (
                  <button
                    className={commonStyles.secondaryButton}
                    type="button"
                    onClick={() => startRollback(revision.data.revision)}
                  >
                    このrevisionを復元
                  </button>
                )}
            </>
          )}
        </section>
      )}
    </div>
  );
}
