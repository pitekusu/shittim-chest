import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getMemorialMemory,
  getMemorialState,
  prepareMemorialUpload,
  queueMemorialGeneration,
  resetMemorial,
  uploadMemorialSource,
} from "../api/memorial";
import { RecordsApiError } from "../api/http";
import type {
  AvatarRef,
  MemorialMemoryResponse,
  MemorialStateResponse,
  MemorialUploadResponse,
  ParticipantSlot,
  RequesterSummary,
} from "../api/types";
import { Avatar } from "../components/Avatar";
import { ErrorPanel } from "../components/ErrorPanel";
import { MemorialEntryTransition } from "../components/MemorialEntryTransition";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import { formatCompletedDateTime } from "../lib/dateTime";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/memorial.module.css";

const ACCEPTED_IMAGE_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const GENERATE_CONFIRMATION = "GENERATE MEMORIAL";
const RESET_CONFIRMATION = "RESET AFFECTION";

const PARTICIPANT_PRESENTATION: Readonly<
  Record<
    ParticipantSlot,
    { readonly name: string; readonly avatar: AvatarRef; readonly color: string }
  >
> = {
  "participant-a": {
    name: "アロナ",
    color: "cyan",
    avatar: {
      kind: "image",
      url: new URL("../../scripts/og-image-assets/participant-a.webp", import.meta.url).href,
      alt: "アロナのアイコン",
      fallbackVariant: "cyan",
    },
  },
  "participant-b": {
    name: "プラナ",
    color: "pink",
    avatar: {
      kind: "image",
      url: new URL("../../scripts/og-image-assets/participant-b.webp", import.meta.url).href,
      alt: "プラナのアイコン",
      fallbackVariant: "pink",
    },
  },
  "participant-c": {
    name: "安倍晋三AI",
    color: "lavender",
    avatar: {
      kind: "image",
      url: new URL("../../scripts/og-image-assets/participant-c.webp", import.meta.url).href,
      alt: "安倍晋三AIのアイコン",
      fallbackVariant: "lavender",
    },
  },
};

type LocalProgress = "idle" | "hashing" | "uploading" | "queueing";

interface GenerationAttempt {
  readonly file: File;
  readonly cycle: number;
  readonly prepareIdempotencyKey: string;
  readonly generateIdempotencyKey: string;
  readonly ticket: MemorialUploadResponse | null;
  readonly uploaded: boolean;
}

function idempotencyKey(): string {
  return `memorial-${crypto.randomUUID()}`;
}

function apiMessage(error: unknown): string {
  if (!(error instanceof RecordsApiError)) {
    return "処理を完了できませんでした。通信状態を確認して、もう一度お試しください。";
  }
  const known: Readonly<Record<string, string>> = {
    MEMORIAL_STATE_CONFLICT: "別の操作で状態が更新されました。最新の状態を読み直しました。",
    MEMORIAL_UPLOAD_REQUIRED:
      "画像のアップロードを確認できませんでした。画像を選び直してください。",
    MEMORIAL_UPLOAD_NOT_ALLOWED: "現在の状態では画像をアップロードできません。",
    MEMORIAL_RECOVERY_REQUIRED: "生成済みデータを確認しています。少し待ってから再開してください。",
    MEMORIAL_GENERATION_ATTEMPTS_EXHAUSTED:
      "自動再試行の上限に達しました。状態を確認してからリセットしてください。",
    MEMORIAL_QUEUE_UNAVAILABLE: "生成の受付が混み合っています。しばらくしてからお試しください。",
    MEMORIAL_RESET_NOT_ALLOWED: "生成処理中は親愛度をリセットできません。",
  };
  return known[error.code] ?? error.message;
}

function validateSelectedFile(file: File): string | null {
  if (!ACCEPTED_IMAGE_TYPES.has(file.type)) {
    return "JPEG、PNG、WebPのいずれかを選んでください。";
  }
  if (file.size < 1 || file.size > MAX_UPLOAD_BYTES) {
    return "画像は10 MiB以下にしてください。";
  }
  return null;
}

function MemorialProgress({
  state,
  localProgress,
}: {
  readonly state: MemorialStateResponse["state"];
  readonly localProgress: LocalProgress;
}): React.JSX.Element | null {
  const stage =
    localProgress === "hashing"
      ? 1
      : localProgress === "uploading"
        ? 1
        : localProgress === "queueing"
          ? 2
          : state === "queued"
            ? 2
            : state === "generating"
              ? 3
              : state === "ready"
                ? 4
                : 0;
  if (stage === 0) return null;
  const complete = stage === 4;
  const message =
    localProgress === "hashing"
      ? "画像を安全に確認しています"
      : localProgress === "uploading"
        ? "画像を一時保管しています"
        : localProgress === "queueing" || state === "queued"
          ? "メモリアル生成を受け付けました"
          : state === "generating"
            ? "思い出を画像と文章にしています"
            : "メモリアルが完成しました";
  return (
    <section className={styles.generationProgress} aria-live="polite" aria-busy={!complete}>
      <div className={styles.progressCopy}>
        <span className={styles.progressPulse} aria-hidden="true" />
        <div>
          <p lang="en">MEMORY SYNTHESIS</p>
          <strong>{message}</strong>
        </div>
        <span>{complete ? "完了" : "進行中"}</span>
      </div>
      <progress
        className={commonStyles.visuallyHidden}
        aria-label="メモリアル生成の進捗"
        value={complete ? 100 : undefined}
        max={100}
      />
      <div className={styles.progressTrack} data-indeterminate={!complete} aria-hidden="true">
        <span />
      </div>
      <ol className={styles.progressSteps} aria-hidden="true">
        <li data-complete={stage >= 1}>画像確認</li>
        <li data-complete={stage >= 2}>生成受付</li>
        <li data-complete={stage >= 3}>思い出生成</li>
        <li data-complete={complete}>完成</li>
      </ol>
    </section>
  );
}

function ConfirmationDialog({
  kind,
  participantName,
  completionFocusRef,
  onCancel,
  onConfirm,
}: {
  readonly kind: "generate" | "reset";
  readonly participantName?: string;
  readonly completionFocusRef: React.RefObject<HTMLHeadingElement | null>;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}): React.JSX.Element {
  const generate = kind === "generate";
  const dialogRef = useRef<HTMLDialogElement>(null);
  const primaryActionRef = useRef<HTMLButtonElement>(null);
  const confirmedRef = useRef(false);

  useEffect(() => {
    const element = dialogRef.current;
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const completionFocus = completionFocusRef.current;
    if (element === null) return;
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    primaryActionRef.current?.focus();
    return () => {
      if (element.open && typeof element.close === "function") element.close();
      const triggerIsUsable =
        previousFocus !== null &&
        previousFocus.isConnected &&
        !(previousFocus instanceof HTMLButtonElement && previousFocus.disabled);
      const focusTarget =
        !confirmedRef.current && triggerIsUsable ? previousFocus : completionFocus;
      focusTarget?.focus();
    };
  }, [completionFocusRef]);

  return (
    <div className={styles.dialogBackdrop}>
      <dialog
        ref={dialogRef}
        className={styles.confirmationDialog}
        aria-labelledby={`${kind}-dialog-title`}
        onCancel={(event) => {
          event.preventDefault();
          onCancel();
        }}
      >
        <span className={styles.dialogIcon} aria-hidden="true">
          {generate ? "✦" : "↺"}
        </span>
        <p className={commonStyles.eyebrow} lang="en">
          {generate ? "ONE-TIME GENERATION" : "RESET AFFECTION"}
        </p>
        <h2 id={`${kind}-dialog-title`}>
          {generate ? "この思い出を一度だけ生成します" : "親愛度をリセットしますか？"}
        </h2>
        {generate ? (
          <ul>
            <li>このcycleで生成に成功できるのは一度だけです。</li>
            <li>選んだ画像をAI生成に使用し、処理後に原本を削除します。</li>
            <li>{participantName}との画像と、思い出を語る文章を生成します。</li>
          </ul>
        ) : (
          <ul>
            <li>3人の親愛度をすべて500点に戻します。</li>
            <li>次の生成には、もう一度誰かとの親愛度を1000点にする必要があります。</li>
            <li>これまでに生成したメモリアルは残ります。</li>
          </ul>
        )}
        <div className={styles.dialogActions}>
          <button className={commonStyles.secondaryButton} type="button" onClick={onCancel}>
            キャンセル
          </button>
          <button
            ref={primaryActionRef}
            className={commonStyles.primaryButton}
            type="button"
            onClick={() => {
              confirmedRef.current = true;
              onConfirm();
            }}
          >
            {generate ? "理解して生成する" : "500点にリセット"}
          </button>
        </div>
      </dialog>
    </div>
  );
}

function MemoryGallery({
  state,
  selectedCycle,
  onSelect,
  memory,
  memoryError,
  onRetryMemory,
}: {
  readonly state: MemorialStateResponse;
  readonly selectedCycle: number | null;
  readonly onSelect: (cycle: number) => void;
  readonly memory: MemorialMemoryResponse | undefined;
  readonly memoryError: unknown;
  readonly onRetryMemory: () => void;
}): React.JSX.Element | null {
  if (state.memories.length === 0) return null;
  return (
    <section className={styles.memoryPanel} aria-labelledby="memory-history-title">
      <header>
        <div>
          <p className={commonStyles.eyebrow} lang="en">
            MEMORY ARCHIVE
          </p>
          <h2 id="memory-history-title">ふたりの思い出</h2>
        </div>
        <span>{state.memories.length} memories</span>
      </header>
      <div className={styles.memoryTabs} role="tablist" aria-label="メモリアル履歴">
        {state.memories.map((item) => {
          const participant = PARTICIPANT_PRESENTATION[item.participant];
          return (
            <button
              key={item.cycle}
              type="button"
              role="tab"
              aria-selected={selectedCycle === item.cycle}
              onClick={() => onSelect(item.cycle)}
            >
              <span>#{item.cycle}</span>
              <strong>{participant.name}</strong>
              <time dateTime={item.generatedAt}>{formatCompletedDateTime(item.generatedAt)}</time>
            </button>
          );
        })}
      </div>
      {memoryError ? (
        <div className={styles.memoryLoading} role="alert">
          <p>{apiMessage(memoryError)}</p>
          <button className={commonStyles.secondaryButton} type="button" onClick={onRetryMemory}>
            もう一度読み込む
          </button>
        </div>
      ) : memory ? (
        <article className={styles.memoryDetail}>
          <figure>
            <img
              src={memory.image.url}
              width={memory.image.width}
              height={memory.image.height}
              alt={memory.image.alt}
              referrerPolicy="no-referrer"
            />
            <figcaption>
              <span lang="en">THE SHITTIM CHEST</span>
              <time dateTime={memory.unlockedAt}>{formatCompletedDateTime(memory.unlockedAt)}</time>
            </figcaption>
          </figure>
          <div className={styles.memoryNarrative}>
            <p className={commonStyles.eyebrow} lang="en">
              OUR STORY
            </p>
            <h3>{PARTICIPANT_PRESENTATION[memory.participant].name}からあなたへ</h3>
            <p>{memory.narrative}</p>
          </div>
        </article>
      ) : (
        <p className={styles.memoryLoading} aria-live="polite">
          思い出を読み込んでいます。
        </p>
      )}
    </section>
  );
}

export default function MemorialPage({
  csrfToken,
  requester,
}: {
  readonly csrfToken: string;
  readonly requester: RequesterSummary;
}): React.JSX.Element {
  const client = useQueryClient();
  const [entryPending, setEntryPending] = useState(true);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [dialog, setDialog] = useState<"generate" | "reset" | null>(null);
  const [localProgress, setLocalProgress] = useState<LocalProgress>("idle");
  const [selectedCycle, setSelectedCycle] = useState<number | null>(null);
  const [generationAttempt, setGenerationAttempt] = useState<GenerationAttempt | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);
  const observedCycleRef = useRef<number | null>(null);
  const generationEpochRef = useRef(0);
  const recoveryGenerationRef = useRef<{
    readonly cycle: number;
    readonly state: "unlocked" | "failed";
    readonly idempotencyKey: string;
  } | null>(null);
  const recoveryResetRef = useRef<{
    readonly cycle: number;
    readonly idempotencyKey: string;
  } | null>(null);

  const stateQuery = useQuery({
    queryKey: ["memorial"],
    queryFn: getMemorialState,
    refetchInterval: (query) => {
      const state = (query.state.data as MemorialStateResponse | undefined)?.state;
      return state === "queued" || state === "generating" ? 3_000 : 30_000;
    },
  });
  useAuthenticationRecovery(stateQuery.error);
  useEffect(() => {
    const recovery = recoveryGenerationRef.current;
    const state = stateQuery.data;
    if (recovery !== null && (state?.cycle !== recovery.cycle || state.state !== recovery.state)) {
      recoveryGenerationRef.current = null;
    }
    if (
      recoveryResetRef.current !== null &&
      state !== undefined &&
      state.cycle !== recoveryResetRef.current.cycle
    ) {
      recoveryResetRef.current = null;
    }
    if (state === undefined) return;
    const observedCycle = observedCycleRef.current;
    if (observedCycle === null) {
      observedCycleRef.current = state.cycle;
      return;
    }
    const cycleAdvanced = state.cycle > observedCycle;
    const generationStateEnded = state.state !== "unlocked" && state.state !== "failed";
    if (!cycleAdvanced && !generationStateEnded) return;
    if (cycleAdvanced) observedCycleRef.current = state.cycle;
    generationEpochRef.current += 1;
    if (fileInputRef.current !== null) fileInputRef.current.value = "";
    setSelectedFile(null);
    setGenerationAttempt(null);
    setFileError(null);
    setActionError(null);
    setDragging(false);
    setDialog(null);
    setLocalProgress("idle");
  }, [stateQuery.data]);

  useEffect(() => {
    const latest = stateQuery.data?.latestReadyCycle;
    if (latest !== null && latest !== undefined) setSelectedCycle(latest);
  }, [stateQuery.data?.latestReadyCycle]);

  const memoryQuery = useQuery({
    queryKey: ["memorial", "memory", selectedCycle],
    queryFn: () => getMemorialMemory(selectedCycle!),
    enabled: selectedCycle !== null,
  });
  useAuthenticationRecovery(memoryQuery.error);

  const refreshAfterConflict = useCallback(
    async (error: unknown) => {
      if (error instanceof RecordsApiError && error.status === 409) {
        await client.invalidateQueries({ queryKey: ["memorial"], exact: true });
      }
    },
    [client],
  );

  const cancelStateRefresh = useCallback(
    () => client.cancelQueries({ queryKey: ["memorial"], exact: true }),
    [client],
  );

  const generation = useMutation({
    mutationFn: async (attempt: GenerationAttempt) => {
      const epoch = generationEpochRef.current;
      const cycleIsCurrent = () => {
        const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
        return (
          generationEpochRef.current === epoch &&
          observedCycleRef.current === attempt.cycle &&
          cached?.cycle === attempt.cycle &&
          (cached.state === "unlocked" || cached.state === "failed")
        );
      };
      if (!cycleIsCurrent()) return null;
      setActionError(null);
      let current = attempt;
      if (current.ticket === null) {
        setLocalProgress("hashing");
        const ticket = await prepareMemorialUpload(
          current.file,
          current.cycle,
          csrfToken,
          current.prepareIdempotencyKey,
        );
        if (!cycleIsCurrent()) return null;
        current = { ...current, ticket };
        setGenerationAttempt(current);
      }
      if (!current.uploaded) {
        if (current.ticket === null) throw new Error("Memorial upload ticket is unavailable");
        setLocalProgress("uploading");
        await uploadMemorialSource(current.ticket, current.file);
        if (!cycleIsCurrent()) return null;
        current = { ...current, uploaded: true };
        setGenerationAttempt(current);
      }
      if (!cycleIsCurrent()) return null;
      setLocalProgress("queueing");
      const next = await queueMemorialGeneration(
        current.cycle,
        GENERATE_CONFIRMATION,
        csrfToken,
        current.generateIdempotencyKey,
      );
      return cycleIsCurrent() ? next : null;
    },
    onSuccess: async (next, attempt) => {
      if (next === null) return;
      await cancelStateRefresh();
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (
        observedCycleRef.current !== attempt.cycle ||
        cached?.cycle !== attempt.cycle ||
        (cached.state !== "unlocked" && cached.state !== "failed")
      ) {
        return;
      }
      if (fileInputRef.current !== null) fileInputRef.current.value = "";
      client.setQueryData(["memorial"], next);
      setSelectedFile(null);
      setGenerationAttempt(null);
      setLocalProgress("idle");
    },
    onError: (error, attempt) => {
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (
        observedCycleRef.current !== attempt.cycle ||
        cached?.cycle !== attempt.cycle ||
        (cached.state !== "unlocked" && cached.state !== "failed")
      ) {
        return;
      }
      setLocalProgress("idle");
      setActionError(apiMessage(error));
      void refreshAfterConflict(error);
    },
  });

  const retryGeneration = useMutation({
    mutationFn: ({ cycle, state }: { cycle: number; state: "unlocked" | "failed" }) => {
      let recovery = recoveryGenerationRef.current;
      if (recovery === null || recovery.cycle !== cycle || recovery.state !== state) {
        recovery = { cycle, state, idempotencyKey: idempotencyKey() };
        recoveryGenerationRef.current = recovery;
      }
      return queueMemorialGeneration(
        cycle,
        GENERATE_CONFIRMATION,
        csrfToken,
        recovery.idempotencyKey,
      );
    },
    onSuccess: async (next, request) => {
      await cancelStateRefresh();
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (cached?.cycle !== request.cycle || cached.state !== request.state) return;
      setActionError(null);
      client.setQueryData(["memorial"], next);
    },
    onError: (error, request) => {
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (cached?.cycle !== request.cycle || cached.state !== request.state) return;
      setActionError(apiMessage(error));
      void refreshAfterConflict(error);
    },
  });

  const reset = useMutation({
    mutationFn: (request: { cycle: number; state: "unlocked" | "failed" | "ready" }) => {
      const { cycle } = request;
      let recovery = recoveryResetRef.current;
      if (recovery === null || recovery.cycle !== cycle) {
        recovery = { cycle, idempotencyKey: idempotencyKey() };
        recoveryResetRef.current = recovery;
      }
      return resetMemorial(cycle, RESET_CONFIRMATION, csrfToken, recovery.idempotencyKey);
    },
    onSuccess: async (next) => {
      await cancelStateRefresh();
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (cached !== undefined && cached.cycle > next.cycle) return;
      setActionError(null);
      if (fileInputRef.current !== null) fileInputRef.current.value = "";
      setSelectedFile(null);
      setGenerationAttempt(null);
      if (cached === undefined || cached.cycle < next.cycle) {
        client.setQueryData(["memorial"], next);
      }
      void client.invalidateQueries({ queryKey: ["affection-rankings"] });
    },
    onError: (error, request) => {
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (cached?.cycle !== request.cycle || cached.state !== request.state) return;
      setActionError(apiMessage(error));
      void refreshAfterConflict(error);
    },
  });
  useAuthenticationRecovery(generation.error);
  useAuthenticationRecovery(retryGeneration.error);
  useAuthenticationRecovery(reset.error);

  const chooseFile = useCallback((file: File | undefined) => {
    if (file === undefined) return;
    const validation = validateSelectedFile(file);
    setFileError(validation);
    setSelectedFile(validation === null ? file : null);
    setGenerationAttempt(null);
    setActionError(null);
  }, []);

  const state = stateQuery.data;
  const participant = state?.unlockedParticipant
    ? PARTICIPANT_PRESENTATION[state.unlockedParticipant]
    : null;
  const busy =
    generation.isPending ||
    retryGeneration.isPending ||
    reset.isPending ||
    state?.state === "queued" ||
    state?.state === "generating";
  const canSelectFile = state?.state === "unlocked" || state?.state === "failed";
  const selectedFileLabel = useMemo(() => {
    if (selectedFile === null) return "画像をドロップ、またはファイルを選択";
    return `${selectedFile.name} · ${(selectedFile.size / 1024 / 1024).toFixed(1)} MiB`;
  }, [selectedFile]);

  useEffect(() => {
    if (!entryPending) pageHeadingRef.current?.focus();
  }, [entryPending]);

  if (stateQuery.isPending) {
    return (
      <section className={styles.loadingPage} aria-live="polite" aria-busy="true">
        <span className={styles.loadingSeal} aria-hidden="true">
          ♥
        </span>
        <p lang="en">MEMORIAL LOBBY</p>
        <h1>思い出を確認しています</h1>
      </section>
    );
  }
  if (stateQuery.isError || state === undefined) {
    const error = stateQuery.error instanceof RecordsApiError ? stateQuery.error : undefined;
    return (
      <ErrorPanel
        title="メモリアルロビーを開けません"
        message={error?.message ?? "しばらくしてから、もう一度お試しください。"}
        requestId={error?.requestId}
        onRetry={() => void stateQuery.refetch()}
      />
    );
  }
  const previouslyOpened = state.resetCount > 0;
  if ((state.state !== "locked" || previouslyOpened) && entryPending) {
    return (
      <MemorialEntryTransition
        requesterName={requester.displayName}
        onComplete={() => setEntryPending(false)}
      />
    );
  }

  return (
    <div className={styles.memorialPage} data-route-motion-ready="">
      <header className={`${commonStyles.pageHeader} ${styles.pageHeader}`}>
        <p className={commonStyles.eyebrow} lang="en">
          MEMORIAL LOBBY
        </p>
        <h1 ref={pageHeadingRef} className={commonStyles.japaneseHeading} tabIndex={-1}>
          メモリアルロビー
        </h1>
        <div className={styles.requesterIdentity}>
          <Avatar avatar={requester.avatar} />
          <span>
            <small>MEMORIAL OWNER</small>
            <strong>{requester.displayName}</strong>
          </span>
        </div>
      </header>

      {state.state === "locked" ? (
        <section className={styles.lockedPanel} aria-labelledby="memorial-locked-title">
          <div className={styles.lockedSeal} aria-hidden="true">
            <span>◇</span>
            <strong>♥</strong>
          </div>
          <div>
            <p className={commonStyles.eyebrow} lang="en">
              ACCESS LOCKED
            </p>
            <h2 id="memorial-locked-title">
              {previouslyOpened
                ? "次のメモリアルロビーはまだ開放されていません"
                : "まだメモリアルロビーにはログインできません"}
            </h2>
            <p>
              {previouslyOpened
                ? state.memories.length > 0
                  ? "これまでの思い出はいつでも閲覧できます。次の開放を目指しましょう。"
                  : "ロビーには入れます。次の開放を目指しましょう。"
                : "3人のうち誰か1人との親愛度が1000点に達すると、特別な思い出を開放できます。"}
            </p>
          </div>
          <span className={styles.cycleBadge}>CYCLE {state.cycle}</span>
        </section>
      ) : (
        <>
          <section
            className={`${styles.unlockPanel} ${participant ? styles[participant.color] : ""}`}
            aria-labelledby="memorial-unlock-title"
          >
            <div className={styles.unlockPortrait}>
              {participant && <Avatar avatar={participant.avatar} />}
              <span aria-hidden="true">1000</span>
            </div>
            <div className={styles.unlockCopy}>
              <p className={commonStyles.eyebrow} lang="en">
                AFFECTION MAX
              </p>
              <h2 id="memorial-unlock-title">{participant?.name}とのロビーが開放されています</h2>
              {state.unlockedAt && (
                <p>
                  達成日{" "}
                  <time dateTime={state.unlockedAt}>
                    {formatCompletedDateTime(state.unlockedAt)}
                  </time>
                </p>
              )}
            </div>
            <div className={styles.heartOrbit} aria-hidden="true">
              <span>♥</span>
              <span>♥</span>
              <span>♥</span>
            </div>
            <span className={styles.cycleBadge}>CYCLE {state.cycle}</span>
          </section>

          <MemorialProgress state={state.state} localProgress={localProgress} />

          {(state.state === "unlocked" || state.state === "failed") && (
            <section className={styles.creationPanel} aria-labelledby="memorial-create-title">
              <header>
                <div>
                  <p className={commonStyles.eyebrow} lang="en">
                    CREATE MEMORY
                  </p>
                  <h2 id="memorial-create-title">ふたりの一枚をつくる</h2>
                </div>
                <span>1920 × 1080</span>
              </header>
              <input
                ref={fileInputRef}
                className={styles.fileInput}
                type="file"
                aria-label="メモリアル用の画像を選択"
                accept="image/jpeg,image/png,image/webp"
                disabled={busy || !canSelectFile || generationAttempt !== null}
                onChange={(event) => chooseFile(event.currentTarget.files?.[0])}
              />
              <button
                type="button"
                className={styles.dropZone}
                data-dragging={dragging}
                data-selected={selectedFile !== null}
                disabled={busy || !canSelectFile || generationAttempt !== null}
                onClick={() => {
                  if (!busy && canSelectFile && generationAttempt === null)
                    fileInputRef.current?.click();
                }}
                onDragEnter={(event) => {
                  event.preventDefault();
                  setDragging(true);
                }}
                onDragOver={(event) => event.preventDefault()}
                onDragLeave={() => setDragging(false)}
                onDrop={(event) => {
                  event.preventDefault();
                  setDragging(false);
                  const droppedFile = event.dataTransfer.files[0];
                  if (
                    droppedFile !== undefined &&
                    !busy &&
                    canSelectFile &&
                    generationAttempt === null
                  ) {
                    if (fileInputRef.current !== null) fileInputRef.current.value = "";
                    chooseFile(droppedFile);
                  }
                }}
              >
                <span className={styles.uploadGlyph} aria-hidden="true">
                  ＋
                </span>
                <strong>{selectedFileLabel}</strong>
                <small>JPEG / PNG / WebP · 最大10 MiB</small>
              </button>
              {fileError && (
                <p className={styles.actionError} role="alert">
                  {fileError}
                </p>
              )}
              <div className={styles.creationActions}>
                <p>選んだ原本は生成処理後に削除され、メモリアルだけが残ります。</p>
                {generationAttempt ? (
                  <div className={styles.retryActions}>
                    <button
                      className={commonStyles.secondaryButton}
                      type="button"
                      disabled={busy}
                      onClick={() => {
                        if (fileInputRef.current !== null) fileInputRef.current.value = "";
                        setSelectedFile(null);
                        setGenerationAttempt(null);
                        setActionError(null);
                      }}
                    >
                      画像を選び直す
                    </button>
                    <button
                      className={commonStyles.primaryButton}
                      type="button"
                      disabled={busy}
                      onClick={() => generation.mutate(generationAttempt)}
                    >
                      {generationAttempt.uploaded
                        ? "生成受付を再試行"
                        : generationAttempt.ticket
                          ? "画像アップロードを再試行"
                          : "生成準備を再試行"}
                    </button>
                  </div>
                ) : (
                  <button
                    className={commonStyles.primaryButton}
                    type="button"
                    disabled={selectedFile === null || busy || !canSelectFile}
                    onClick={() => setDialog("generate")}
                  >
                    メモリアルロビーを開放
                  </button>
                )}
              </div>
              {state.state === "unlocked" &&
                state.uploadReady &&
                generationAttempt === null &&
                selectedFile === null && (
                  <button
                    className={`${commonStyles.secondaryButton} ${styles.retryButton}`}
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      retryGeneration.mutate({ cycle: state.cycle, state: "unlocked" })
                    }
                  >
                    準備済みの画像で生成を続ける
                  </button>
                )}
              {state.state === "failed" && (
                <button
                  className={`${commonStyles.secondaryButton} ${styles.retryButton}`}
                  type="button"
                  disabled={busy}
                  onClick={() => retryGeneration.mutate({ cycle: state.cycle, state: "failed" })}
                >
                  前回の生成を再開
                </button>
              )}
            </section>
          )}
        </>
      )}

      {actionError && (
        <p className={styles.actionError} role="alert">
          {actionError}
        </p>
      )}

      <MemoryGallery
        state={state}
        selectedCycle={selectedCycle}
        onSelect={setSelectedCycle}
        memory={memoryQuery.data}
        memoryError={memoryQuery.error}
        onRetryMemory={() => void memoryQuery.refetch()}
      />

      {state.state !== "locked" && state.state !== "queued" && state.state !== "generating" && (
        <section className={styles.resetPanel} aria-labelledby="memorial-reset-title">
          <div>
            <p className={commonStyles.eyebrow} lang="en">
              NEW CYCLE
            </p>
            <h2 id="memorial-reset-title">新しい思い出をはじめる</h2>
            <p>親愛度を500点へ戻すと、次のメモリアル開放を目指せます。</p>
          </div>
          <button
            className={commonStyles.secondaryButton}
            type="button"
            disabled={busy}
            onClick={() => setDialog("reset")}
          >
            親愛度をリセット
          </button>
        </section>
      )}

      {dialog === "generate" && participant && selectedFile && (
        <ConfirmationDialog
          kind="generate"
          participantName={participant.name}
          completionFocusRef={pageHeadingRef}
          onCancel={() => setDialog(null)}
          onConfirm={() => {
            setDialog(null);
            const attempt: GenerationAttempt = {
              file: selectedFile,
              cycle: state.cycle,
              prepareIdempotencyKey: idempotencyKey(),
              generateIdempotencyKey: idempotencyKey(),
              ticket: null,
              uploaded: false,
            };
            setGenerationAttempt(attempt);
            generation.mutate(attempt);
          }}
        />
      )}
      {dialog === "reset" && (
        <ConfirmationDialog
          kind="reset"
          completionFocusRef={pageHeadingRef}
          onCancel={() => setDialog(null)}
          onConfirm={() => {
            setDialog(null);
            if (state.state === "unlocked" || state.state === "failed" || state.state === "ready") {
              reset.mutate({ cycle: state.cycle, state: state.state });
            }
          }}
        />
      )}
    </div>
  );
}
