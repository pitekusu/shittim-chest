import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type JSX } from "react";

import {
  applyAdminPrompts,
  getAdminPrompts,
  getAdminRevision,
  getAdminRevisions,
  getAdminStatus,
  rollbackAdminPrompts,
} from "../api/admin";
import { RecordsApiError } from "../api/http";
import type {
  AdminApplyRequest,
  AdminPromptKey,
  AdminPrompts,
  AdminPromptsResponse,
  AdminRevisionsResponse,
  AdminRollbackRequest,
} from "../api/types";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import {
  ADMIN_PROMPT_APPLICATION_LABELS,
  deriveAdminPromptApplicationState,
} from "../lib/adminPromptState";
import { formatCompletedDateTime } from "../lib/dateTime";
import { lineDiff } from "../lib/lineDiff";
import adminStyles from "../styles/admin.module.css";
import commonStyles from "../styles/common.module.css";

const PROMPT_LIMIT_BYTES = 3_500;
const SYSTEM_CONFIRMATION = "APPLY SYSTEM PROMPT";
const PROMPT_KEYS: readonly AdminPromptKey[] = [
  "system",
  "moderator",
  "participantA",
  "participantB",
  "participantC",
];

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

interface RetainedIdempotencyKey {
  readonly payload: string;
  readonly value: string;
}

function idempotencyKeyFor(
  reference: { current: RetainedIdempotencyKey | null },
  request: AdminApplyRequest | AdminRollbackRequest,
): string {
  const payload = JSON.stringify(request);
  if (reference.current?.payload !== payload) {
    reference.current = { payload, value: crypto.randomUUID() };
  }
  return reference.current.value;
}

function normalizePrompt(value: string): string {
  return value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").normalize("NFC");
}

function normalizePrompts(prompts: AdminPrompts): AdminPrompts {
  return {
    system: normalizePrompt(prompts.system),
    moderator: normalizePrompt(prompts.moderator),
    participantA: normalizePrompt(prompts.participantA),
    participantB: normalizePrompt(prompts.participantB),
    participantC: normalizePrompt(prompts.participantC),
  };
}

function promptBytes(value: string): number {
  return new TextEncoder().encode(normalizePrompt(value)).byteLength;
}

function promptIsValid(value: string): boolean {
  const normalized = normalizePrompt(value);
  return normalized.trim().length > 0 && promptBytes(normalized) <= PROMPT_LIMIT_BYTES;
}

function promptsEqual(left: AdminPrompts, right: AdminPrompts): boolean {
  const normalizedLeft = normalizePrompts(left);
  const normalizedRight = normalizePrompts(right);
  return PROMPT_KEYS.every((key) => normalizedLeft[key] === normalizedRight[key]);
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

function isRevisionConflict(error: unknown): boolean {
  return (
    error instanceof RecordsApiError &&
    error.status === 409 &&
    error.code === "PROMPT_REVISION_CONFLICT"
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
}): JSX.Element {
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
        <span data-kind="removed">− 現在</span>
        <span data-kind="added">＋ 選択revision</span>
      </div>
      <div className={adminStyles.promptDiffRows}>
        <table className={adminStyles.promptDiffTable} aria-label="現在と選択revisionの行差分">
          <tbody>
            {entries.map((entry, index) => (
              <tr className={adminStyles.promptDiffLine} data-kind={entry.kind} key={index}>
                <td
                  aria-label={
                    entry.beforeLine === null ? "現在の行なし" : `現在 ${entry.beforeLine}行目`
                  }
                >
                  {entry.beforeLine ?? ""}
                </td>
                <td
                  aria-label={
                    entry.afterLine === null
                      ? "選択revisionの行なし"
                      : `選択revision ${entry.afterLine}行目`
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
  const queryClient = useQueryClient();
  const prompts = useQuery({ queryKey: ["admin", "prompts"], queryFn: getAdminPrompts });
  const status = useQuery({ queryKey: ["admin", "status"], queryFn: getAdminStatus });
  const revisions = useInfiniteQuery<AdminRevisionsResponse>({
    queryKey: ["admin", "prompt-revisions"],
    queryFn: ({ pageParam }) => getAdminRevisions(pageParam as string | undefined),
    initialPageParam: undefined,
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  });
  const [selectedPrompt, setSelectedPrompt] = useState<AdminPromptKey>("system");
  const [baseSnapshot, setBaseSnapshot] = useState<AdminPromptsResponse | null>(null);
  const [drafts, setDrafts] = useState<AdminPrompts | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [selectedRevision, setSelectedRevision] = useState<string | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<string | null>(null);
  const [rollbackConfirmation, setRollbackConfirmation] = useState("");
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [latestConflict, setLatestConflict] = useState<AdminPromptsResponse | null>(null);
  const applyIdempotencyKeyRef = useRef<RetainedIdempotencyKey | null>(null);
  const rollbackIdempotencyKeyRef = useRef<RetainedIdempotencyKey | null>(null);

  const revision = useQuery({
    queryKey: ["admin", "prompt-revision", selectedRevision],
    queryFn: () => {
      if (selectedRevision === null) throw new Error("admin_prompt_revision_unavailable");
      return getAdminRevision(selectedRevision);
    },
    enabled: selectedRevision !== null,
  });

  useAuthenticationRecovery(prompts.error);
  useAuthenticationRecovery(status.error);
  useAuthenticationRecovery(revisions.error);
  useAuthenticationRecovery(revision.error);

  useEffect(() => {
    if (baseSnapshot !== null || prompts.data === undefined) return;
    setBaseSnapshot(prompts.data);
    setDrafts(prompts.data.prompts);
  }, [baseSnapshot, prompts.data]);

  async function loadLatestAfterConflict(): Promise<void> {
    const latest = await prompts.refetch();
    if (latest.data !== undefined) setLatestConflict(latest.data);
  }

  async function loadSavedRevision(message: string): Promise<void> {
    setSelectedRevision(null);
    setRollbackTarget(null);
    setRollbackConfirmation("");
    rollbackIdempotencyKeyRef.current = null;
    queryClient.removeQueries({ queryKey: ["admin", "prompt-revision"] });
    const latest = await prompts.refetch();
    if (latest.data !== undefined) {
      setBaseSnapshot(latest.data);
      setDrafts(latest.data.prompts);
    }
    setLatestConflict(null);
    setConfirmation("");
    setSuccessMessage(message);
    await queryClient.invalidateQueries({ queryKey: ["admin", "prompt-revisions"] });
  }

  const applyMutation = useMutation({
    mutationFn: async () => {
      if (baseSnapshot === null || drafts === null) {
        throw new Error("admin_prompt_state_unavailable");
      }
      const normalizedDrafts = normalizePrompts(drafts);
      const request: AdminApplyRequest = {
        schemaVersion: 1,
        baseRevision: baseSnapshot.activeRevision,
        prompts: normalizedDrafts,
        systemConfirmation:
          normalizedDrafts.system === normalizePrompt(baseSnapshot.prompts.system)
            ? null
            : confirmation,
      };
      const response = await applyAdminPrompts(
        request,
        csrfToken,
        idempotencyKeyFor(applyIdempotencyKeyRef, request),
      );
      return response;
    },
    onSuccess: async (response) => {
      applyIdempotencyKeyRef.current = null;
      await loadSavedRevision(`revision ${response.revision} を保存しました。`);
    },
    onError: async (error) => {
      if (isRevisionConflict(error)) await loadLatestAfterConflict();
    },
  });
  useAuthenticationRecovery(applyMutation.error);

  const rollbackMutation = useMutation({
    mutationFn: async () => {
      if (
        baseSnapshot === null ||
        baseSnapshot.activeRevision === null ||
        rollbackTarget === null
      ) {
        throw new Error("admin_prompt_revision_unavailable");
      }
      const request: AdminRollbackRequest = {
        schemaVersion: 1,
        baseRevision: baseSnapshot.activeRevision,
        sourceRevision: rollbackTarget,
        systemConfirmation: rollbackSystemChanged ? rollbackConfirmation : null,
      };
      return rollbackAdminPrompts(
        request,
        csrfToken,
        idempotencyKeyFor(rollbackIdempotencyKeyRef, request),
      );
    },
    onSuccess: async (response) => {
      await loadSavedRevision(`revision ${response.revision} を復元版として保存しました。`);
    },
    onError: async (error) => {
      if (isRevisionConflict(error)) await loadLatestAfterConflict();
    },
  });
  useAuthenticationRecovery(rollbackMutation.error);

  const normalizedDrafts = drafts === null ? null : normalizePrompts(drafts);
  const systemChanged = Boolean(
    baseSnapshot !== null &&
    normalizedDrafts !== null &&
    normalizePrompt(baseSnapshot.prompts.system) !== normalizedDrafts.system,
  );
  const dirty = Boolean(
    baseSnapshot !== null && drafts !== null && !promptsEqual(baseSnapshot.prompts, drafts),
  );
  const invalidPrompts =
    drafts === null ? PROMPT_KEYS : PROMPT_KEYS.filter((key) => !promptIsValid(drafts[key]));
  const isLegacyRegistration = baseSnapshot?.mode === "legacy" && !dirty;
  const canApply =
    canWrite &&
    baseSnapshot !== null &&
    drafts !== null &&
    invalidPrompts.length === 0 &&
    (dirty || baseSnapshot.mode === "legacy") &&
    (!systemChanged || confirmation === SYSTEM_CONFIRMATION) &&
    !applyMutation.isPending;
  const allRevisions = revisions.data?.pages.flatMap((page) => page.items) ?? [];
  const selectedIsCurrent =
    revision.data !== undefined && revision.data.revision === prompts.data?.activeRevision;
  const rollbackSameContent = Boolean(
    revision.data !== undefined &&
    prompts.data !== undefined &&
    promptsEqual(revision.data.prompts, prompts.data.prompts),
  );
  const rollbackSystemChanged = Boolean(
    rollbackTarget !== null &&
    revision.data?.revision === rollbackTarget &&
    prompts.data !== undefined &&
    normalizePrompt(prompts.data.prompts.system) !== normalizePrompt(revision.data.prompts.system),
  );
  const canRollback =
    canWrite &&
    rollbackTarget !== null &&
    revision.data?.revision === rollbackTarget &&
    baseSnapshot?.activeRevision != null &&
    !rollbackSameContent &&
    (!rollbackSystemChanged || rollbackConfirmation === SYSTEM_CONFIRMATION) &&
    !rollbackMutation.isPending;
  const applicationState = deriveAdminPromptApplicationState(prompts.data, status.data);

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

      <section className={adminStyles.adminPanel} aria-labelledby="prompt-editor-title">
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
        {prompts.isError && (
          <PanelState
            title="プロンプトを読み込めませんでした"
            message={errorMessage(prompts.error, "通信状態を確認してください。")}
            onRetry={() => void prompts.refetch()}
          />
        )}
        {!prompts.isPending && !prompts.isError && baseSnapshot !== null && drafts !== null && (
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
                  onClick={() => {
                    setBaseSnapshot(latestConflict);
                    setLatestConflict(null);
                    applyIdempotencyKeyRef.current = null;
                    rollbackIdempotencyKeyRef.current = null;
                    applyMutation.reset();
                    rollbackMutation.reset();
                  }}
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
                onChange={(event) => {
                  if (!canWrite) return;
                  applyIdempotencyKeyRef.current = null;
                  applyMutation.reset();
                  setSuccessMessage(null);
                  setDrafts({ ...drafts, [selectedPrompt]: event.target.value });
                }}
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
                  <div>
                    <strong className={adminStyles.promptRevision}>{item.revision}</strong>
                    <span>
                      {item.action === "rollback" ? "復元" : "更新"}・
                      {formatCompletedDateTime(item.createdAt)}
                    </span>
                    <small>
                      元revision: {item.sourceRevision ?? item.baseRevision ?? "既存設定"}
                    </small>
                  </div>
                  <span className={adminStyles.promptChecksum}>
                    checksum {item.checksum.slice(0, 12)}
                  </span>
                  <div className={adminStyles.promptHistoryActions}>
                    {isCurrent ? (
                      <span className={adminStyles.promptCurrentBadge}>使用中</span>
                    ) : (
                      <>
                        <button
                          className={commonStyles.secondaryButton}
                          type="button"
                          onClick={() => setSelectedRevision(item.revision)}
                        >
                          変更点を見る
                        </button>
                        {canWrite && (
                          <button
                            className={commonStyles.secondaryButton}
                            type="button"
                            onClick={() => {
                              rollbackIdempotencyKeyRef.current = null;
                              rollbackMutation.reset();
                              setSelectedRevision(item.revision);
                              setRollbackTarget(item.revision);
                              setRollbackConfirmation("");
                            }}
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

        {selectedRevision !== null && (
          <section
            className={adminStyles.promptRevisionDetail}
            aria-labelledby="prompt-revision-detail-title"
          >
            <header>
              <div>
                <h3 id="prompt-revision-detail-title">revisionを比較</h3>
                <p className={adminStyles.promptRevision}>{selectedRevision}</p>
              </div>
              <button
                className={commonStyles.secondaryButton}
                type="button"
                onClick={() => {
                  setSelectedRevision(null);
                  setRollbackTarget(null);
                  setRollbackConfirmation("");
                  rollbackIdempotencyKeyRef.current = null;
                  rollbackMutation.reset();
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
                        before={prompts.data.prompts[key]}
                        after={revision.data.prompts[key]}
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
                        onClick={() => {
                          setRollbackTarget(null);
                          setRollbackConfirmation("");
                          rollbackIdempotencyKeyRef.current = null;
                          rollbackMutation.reset();
                        }}
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
                      onClick={() => {
                        rollbackIdempotencyKeyRef.current = null;
                        rollbackMutation.reset();
                        setRollbackTarget(revision.data.revision);
                      }}
                    >
                      このrevisionを復元
                    </button>
                  )}
              </>
            )}
          </section>
        )}
      </section>
    </div>
  );
}
