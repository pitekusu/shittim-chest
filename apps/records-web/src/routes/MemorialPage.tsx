import { useEffect, useRef, useState } from "react";

import { getMemorialMemory } from "../api/memorial";
import { RecordsApiError } from "../api/http";
import type {
  AvatarRef,
  MemorialMemoryResponse,
  MemorialStateResponse,
  ParticipantSlot,
  RequesterSummary,
} from "../api/types";
import { Avatar } from "../components/Avatar";
import { ErrorPanel } from "../components/ErrorPanel";
import { MemorialEntryTransition } from "../components/MemorialEntryTransition";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import {
  memorialActionMessage,
  useMemorialController,
  type LocalProgress,
} from "../hooks/useMemorialController";
import { formatCompletedDateTime } from "../lib/dateTime";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/memorial.module.css";

const GENERATION_STEPS = ["画像確認", "生成受付", "思い出生成", "完成"] as const;

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

function memoryTabTarget(
  memories: MemorialStateResponse["memories"],
  currentCycle: number,
  key: string,
): number | null {
  const currentIndex = memories.findIndex((memory) => memory.cycle === currentCycle);
  if (currentIndex < 0) return null;
  if (key === "Home") return memories[0]?.cycle ?? null;
  if (key === "End") return memories.at(-1)?.cycle ?? null;
  if (key === "ArrowRight" || key === "ArrowDown") {
    return memories[(currentIndex + 1) % memories.length]?.cycle ?? null;
  }
  if (key === "ArrowLeft" || key === "ArrowUp") {
    return memories[(currentIndex - 1 + memories.length) % memories.length]?.cycle ?? null;
  }
  return null;
}

function SelectedImagePreview({ file }: { readonly file: File }): React.JSX.Element {
  const [preview, setPreview] = useState<{ readonly file: File; readonly url: string } | null>(
    null,
  );
  const [failedFile, setFailedFile] = useState<File | null>(null);

  useEffect(() => {
    const reader = new FileReader();
    reader.onload = () => {
      if (typeof reader.result === "string") setPreview({ file, url: reader.result });
      else setFailedFile(file);
    };
    reader.onerror = () => setFailedFile(file);
    reader.readAsDataURL(file);
    return () => {
      reader.onload = null;
      reader.onerror = null;
      if (reader.readyState === FileReader.LOADING) reader.abort();
    };
  }, [file]);

  if (failedFile === file) return <small>画像のプレビューを表示できません。</small>;
  if (preview?.file !== file) return <small>プレビューを読み込んでいます。</small>;
  return (
    <img
      className={styles.uploadPreview}
      src={preview.url}
      width={160}
      height={160}
      alt="選択した画像のプレビュー"
      onError={() => setFailedFile(file)}
    />
  );
}

function UnlockOrbit(): React.JSX.Element {
  const orbitRef = useRef<HTMLDivElement>(null);
  const [animating, setAnimating] = useState(false);
  useEffect(() => {
    const orbit = orbitRef.current;
    if (orbit === null) return;
    let inViewport = false;
    const update = () => setAnimating(inViewport && document.visibilityState !== "hidden");
    const observer =
      typeof IntersectionObserver === "function"
        ? new IntersectionObserver(([entry]) => {
            inViewport = entry?.isIntersecting ?? false;
            update();
          })
        : null;
    if (observer) observer.observe(orbit);
    else {
      inViewport = true;
      update();
    }
    document.addEventListener("visibilitychange", update);
    return () => {
      observer?.disconnect();
      document.removeEventListener("visibilitychange", update);
    };
  }, []);
  return (
    <div ref={orbitRef} className={styles.heartOrbit} data-animating={animating} aria-hidden="true">
      <span>♥</span>
      <span>♥</span>
      <span>♥</span>
    </div>
  );
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
            ? "ふたりの思い出をつくっています"
            : "メモリアルが完成しました";
  return (
    <section className={styles.generationProgress} aria-live="polite" aria-busy={!complete}>
      <div className={styles.progressCopy}>
        <span className={styles.progressPulse} aria-hidden="true" />
        <div>
          <p lang="en">MEMORY SYNTHESIS</p>
          <strong>{message}</strong>
        </div>
        <span>{complete ? "完了" : `${stage} / 4 工程`}</span>
      </div>
      {!complete && (
        <p className={styles.generationEstimate}>
          生成には<strong>3分程度</strong>かかります。
          <span>状況により前後します。</span>
        </p>
      )}
      <progress
        className={commonStyles.visuallyHidden}
        aria-label="メモリアル生成の進捗"
        aria-valuetext={`${stage} / 4 工程：${message}`}
        value={complete ? 100 : undefined}
        max={100}
      />
      <div className={styles.progressTrack} aria-hidden="true">
        {GENERATION_STEPS.map((label, index) => (
          <span
            key={label}
            data-complete={complete || stage > index + 1}
            data-current={!complete && stage === index + 1}
          >
            <span />
          </span>
        ))}
      </div>
      <ol className={styles.progressSteps} aria-label="生成の工程">
        {GENERATION_STEPS.map((label, index) => (
          <li
            key={label}
            data-complete={complete || stage > index + 1}
            aria-current={!complete && stage === index + 1 ? "step" : undefined}
          >
            {label}
            <span className={commonStyles.visuallyHidden}>
              {complete || stage > index + 1
                ? "（完了）"
                : stage === index + 1
                  ? "（進行中）"
                  : "（待機）"}
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ConfirmationDialog({
  kind,
  participantName,
  completionFocusRef,
  pointerInitiated,
  confirmDisabled,
  onCancel,
  onConfirm,
}: {
  readonly kind: "generate" | "reset";
  readonly participantName?: string;
  readonly completionFocusRef: React.RefObject<HTMLHeadingElement | null>;
  readonly pointerInitiated: boolean;
  readonly confirmDisabled: boolean;
  readonly onCancel: () => void;
  readonly onConfirm: () => void;
}): React.JSX.Element {
  const generate = kind === "generate";
  const dialogRef = useRef<HTMLDialogElement>(null);
  const cancelActionRef = useRef<HTMLButtonElement>(null);
  const confirmedRef = useRef(false);

  useEffect(() => {
    const element = dialogRef.current;
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const completionFocus = completionFocusRef.current;
    if (element === null) return;
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    cancelActionRef.current?.focus();
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
        data-pointer-initiated={pointerInitiated}
        aria-labelledby={`${kind}-dialog-title`}
        onCancel={(event) => {
          event.preventDefault();
          onCancel();
        }}
      >
        <span className={styles.dialogIcon} aria-hidden="true">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            {generate ? (
              <path
                fill="currentColor"
                d="M12 1.5c1.8 6.2 4.3 8.7 10.5 10.5-6.2 1.8-8.7 4.3-10.5 10.5C10.2 16.3 7.7 13.8 1.5 12 7.7 10.2 10.2 7.7 12 1.5Z"
              />
            ) : (
              <g
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M4.6 7.2A8 8 0 1 1 4 14" />
                <path d="M4 3v5h5" />
              </g>
            )}
          </svg>
        </span>
        <p className={commonStyles.eyebrow} lang="en">
          {generate ? "ONE-TIME GENERATION" : "RESET AFFECTION"}
        </p>
        <h2 id={`${kind}-dialog-title`}>
          {generate ? "思い出を一度だけ生成します。" : "親愛度をリセットしますか？"}
        </h2>
        {generate ? (
          <ul>
            <li>
              このメモリアルロビーで生成できる{participantName}との思い出は
              <strong className={styles.onceOnly}>一度だけ</strong>です。
            </li>
            <li>選んだ画像が{participantName}との思い出の生成に使用されます。</li>
          </ul>
        ) : (
          <ul>
            <li>3人の親愛度をすべて500点に戻します。</li>
            <li>次の生成には、もう一度誰かとの親愛度を1000点にする必要があります。</li>
            <li>これまでに生成したメモリアルは残ります。</li>
          </ul>
        )}
        <div className={styles.dialogActions}>
          <button
            ref={cancelActionRef}
            className={commonStyles.secondaryButton}
            type="button"
            onClick={onCancel}
          >
            キャンセル
          </button>
          <button
            className={commonStyles.primaryButton}
            type="button"
            disabled={confirmDisabled}
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

function MemoryArtwork({
  memory,
  onRetryMemory,
}: {
  readonly memory: MemorialMemoryResponse;
  readonly onRetryMemory: () => Promise<boolean>;
}): React.JSX.Element {
  const [imageFailed, setImageFailed] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [imageAttempt, setImageAttempt] = useState(0);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<unknown>(null);
  const mounted = useRef(true);
  useAuthenticationRecovery(downloadError);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const downloadImage = async () => {
    setDownloading(true);
    setDownloadError(null);
    try {
      // Refresh the owner-authorized URL; a displayed image can outlive its signature.
      const freshMemory = await getMemorialMemory(memory);
      if (!mounted.current) return;
      const link = document.createElement("a");
      link.href = freshMemory.image.url;
      link.download = `the-shittim-chest-memorial-${memory.cycle}.png`;
      link.rel = "noreferrer";
      link.setAttribute("referrerpolicy", "no-referrer");
      document.body.append(link);
      link.click();
      link.remove();
    } catch (error) {
      if (mounted.current) setDownloadError(error);
    } finally {
      if (mounted.current) setDownloading(false);
    }
  };

  const retryImage = async () => {
    setRetrying(true);
    try {
      if (await onRetryMemory()) {
        setImageAttempt((attempt) => attempt + 1);
        setImageFailed(false);
      }
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className={styles.memoryArtwork}>
      <figure>
        {imageFailed ? (
          <div className={styles.memoryImageError} role="alert" aria-busy={retrying}>
            <p>
              画像を読み込めませんでした。通信状態や画像URLの有効期限を確認するため、再取得してください。
            </p>
            <button
              className={commonStyles.secondaryButton}
              type="button"
              disabled={retrying}
              onClick={() => void retryImage()}
            >
              {retrying ? "画像を再取得しています" : "画像を再取得"}
            </button>
            <small>完成した画像だけを読み直します。再生成は行いません。</small>
          </div>
        ) : (
          <>
            <img
              key={imageAttempt}
              src={memory.image.url}
              width={memory.image.width}
              height={memory.image.height}
              alt={memory.image.alt}
              referrerPolicy="no-referrer"
              onError={() => setImageFailed(true)}
            />
            <figcaption>
              <span lang="en">THE SHITTIM CHEST</span>
              <time dateTime={memory.unlockedAt}>{formatCompletedDateTime(memory.unlockedAt)}</time>
            </figcaption>
          </>
        )}
      </figure>
      <div className={styles.memoryDownload}>
        <button
          className={commonStyles.secondaryButton}
          type="button"
          disabled={downloading || retrying}
          onClick={() => void downloadImage()}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            aria-hidden="true"
          >
            <path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5" />
          </svg>
          {downloading ? "保存を準備しています" : "画像を保存"}
        </button>
        {downloadError !== null && <p role="alert">{memorialActionMessage(downloadError)}</p>}
      </div>
    </div>
  );
}

function MemoryGallery({
  state,
  selectedCycle,
  onSelect,
  memory,
  memoryError,
  memoryUpdatedAt,
  onRetryMemory,
}: {
  readonly state: MemorialStateResponse;
  readonly selectedCycle: number | null;
  readonly onSelect: (cycle: number) => void;
  readonly memory: MemorialMemoryResponse | undefined;
  readonly memoryError: unknown;
  readonly memoryUpdatedAt: number;
  readonly onRetryMemory: () => Promise<boolean>;
}): React.JSX.Element | null {
  if (state.memories.length === 0) return null;
  const effectiveSelectedCycle =
    state.memories.find((item) => item.cycle === selectedCycle)?.cycle ??
    state.memories.find((item) => item.cycle === state.latestReadyCycle)?.cycle ??
    state.memories.at(-1)!.cycle;
  const selectedTabId = `memorial-memory-tab-${effectiveSelectedCycle}`;
  const tabPanelProps = {
    id: "memorial-memory-panel",
    role: "tabpanel" as const,
    "aria-labelledby": selectedTabId,
  };
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
              id={`memorial-memory-tab-${item.cycle}`}
              type="button"
              role="tab"
              aria-controls="memorial-memory-panel"
              aria-selected={effectiveSelectedCycle === item.cycle}
              tabIndex={effectiveSelectedCycle === item.cycle ? 0 : -1}
              onClick={() => onSelect(item.cycle)}
              onKeyDown={(event) => {
                const target = memoryTabTarget(state.memories, item.cycle, event.key);
                if (target === null) return;
                event.preventDefault();
                onSelect(target);
                document.querySelector<HTMLElement>(`#memorial-memory-tab-${target}`)?.focus();
              }}
            >
              <span>{item.cycle}回目</span>
              <strong>{participant.name}</strong>
              <time dateTime={item.generatedAt}>{formatCompletedDateTime(item.generatedAt)}</time>
            </button>
          );
        })}
      </div>
      {memoryError && !memory ? (
        <div {...tabPanelProps}>
          <div className={styles.memoryLoading} role="alert">
            <p>{memorialActionMessage(memoryError)}</p>
            <button
              className={commonStyles.secondaryButton}
              type="button"
              onClick={() => void onRetryMemory()}
            >
              もう一度読み込む
            </button>
          </div>
        </div>
      ) : memory ? (
        <div {...tabPanelProps}>
          {memoryError != null && (
            <div className={styles.refreshError} role="alert">
              <p>{memorialActionMessage(memoryError)}</p>
              <button
                className={commonStyles.secondaryButton}
                type="button"
                onClick={() => void onRetryMemory()}
              >
                もう一度読み込む
              </button>
            </div>
          )}
          <article className={styles.memoryDetail}>
            <MemoryArtwork
              key={`${memory.cycle}:${memory.image.url}:${memoryUpdatedAt}`}
              memory={memory}
              onRetryMemory={onRetryMemory}
            />
            <div className={styles.memoryNarrative}>
              <p className={commonStyles.eyebrow} lang="en">
                OUR STORY
              </p>
              <h3>{PARTICIPANT_PRESENTATION[memory.participant].name}からあなたへ</h3>
              <p>{memory.narrative}</p>
            </div>
          </article>
        </div>
      ) : (
        <div {...tabPanelProps}>
          <p className={styles.memoryLoading} aria-live="polite">
            思い出を読み込んでいます。
          </p>
        </div>
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
  const [entryPending, setEntryPending] = useState(true);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);
  const {
    stateQuery,
    memoryQuery,
    selectedFile,
    selectedFileLabel,
    fileError,
    actionError,
    dragging,
    setDragging,
    dialog,
    localProgress,
    selectedCycle,
    setSelectedCycle,
    generationAttempt,
    fileInputRef,
    busy,
    canSelectFile,
    actionsBlocked,
    chooseFile,
    openConfirmation,
    cancelConfirmation,
    confirmGeneration,
    confirmReset,
    discardAttempt,
    retryAttempt,
    resumeGeneration,
  } = useMemorialController(csrfToken);
  const state = stateQuery.data;
  const participant = state?.unlockedParticipant
    ? PARTICIPANT_PRESENTATION[state.unlockedParticipant]
    : null;

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
  if (state === undefined) {
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
      {stateQuery.isError && (
        <div className={styles.refreshError} role="alert">
          <div>
            <strong>最新の状態を確認できませんでした</strong>
            <p>{memorialActionMessage(stateQuery.error)}</p>
            <p>状態を確認できるまで、生成と親愛度のリセットは利用できません。</p>
          </div>
          <button
            className={commonStyles.secondaryButton}
            type="button"
            disabled={stateQuery.isFetching}
            onClick={() => void stateQuery.refetch()}
          >
            {stateQuery.isFetching ? "状態を確認しています" : "最新の状態を確認"}
          </button>
        </div>
      )}

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
            <h2
              id="memorial-locked-title"
              className={`${commonStyles.japaneseText} ${commonStyles.japaneseHeading}`}
            >
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
          <span className={styles.cycleBadge}>{state.cycle}回目</span>
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
              <h2
                id="memorial-unlock-title"
                className={`${commonStyles.japaneseText} ${commonStyles.japaneseHeading}`}
              >
                {participant?.name}とのメモリアルロビーが解放されました
              </h2>
              {state.unlockedAt && (
                <p>
                  達成日{" "}
                  <time dateTime={state.unlockedAt}>
                    {formatCompletedDateTime(state.unlockedAt)}
                  </time>
                </p>
              )}
            </div>
            <UnlockOrbit />
            <span className={styles.cycleBadge}>{state.cycle}回目</span>
          </section>

          <MemorialProgress state={state.state} localProgress={localProgress} />

          {state.state === "queued" && (
            <section className={styles.creationPanel} aria-label="生成受付の再送">
              <p>受付後も生成が始まらない場合は、同じ生成依頼を再送できます。</p>
              <button
                className={commonStyles.secondaryButton}
                type="button"
                disabled={actionsBlocked}
                onClick={() => resumeGeneration("queued")}
              >
                生成受付を再送
              </button>
              <p>確認済みの依頼を再送するだけで、別の作品は生成しません。</p>
            </section>
          )}

          {(state.state === "unlocked" || state.state === "failed") && (
            <section className={styles.creationPanel} aria-labelledby="memorial-create-title">
              <header>
                <div>
                  <p className={commonStyles.eyebrow} lang="en">
                    CREATE MEMORY
                  </p>
                  <h2 id="memorial-create-title">ふたりの一枚をつくる</h2>
                </div>
              </header>
              <input
                ref={fileInputRef}
                className={styles.fileInput}
                type="file"
                tabIndex={-1}
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
                {selectedFile ? (
                  <SelectedImagePreview file={selectedFile} />
                ) : (
                  <span className={styles.uploadGlyph} aria-hidden="true">
                    ＋
                  </span>
                )}
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
                      onClick={discardAttempt}
                    >
                      画像を選び直す
                    </button>
                    <button
                      className={commonStyles.primaryButton}
                      type="button"
                      disabled={busy || actionsBlocked}
                      onClick={retryAttempt}
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
                    disabled={selectedFile === null || busy || !canSelectFile || actionsBlocked}
                    onClick={(event) => openConfirmation("generate", event.detail > 0)}
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
                    disabled={busy || actionsBlocked}
                    onClick={() => resumeGeneration("unlocked")}
                  >
                    準備済みの画像で生成を続ける
                  </button>
                )}
              {state.state === "failed" && generationAttempt === null && (
                <button
                  className={`${commonStyles.secondaryButton} ${styles.retryButton}`}
                  type="button"
                  disabled={busy || actionsBlocked}
                  onClick={() => resumeGeneration("failed")}
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
        memoryUpdatedAt={memoryQuery.dataUpdatedAt}
        onRetryMemory={async () => (await memoryQuery.refetch()).isSuccess}
      />

      {state.state !== "locked" && state.state !== "queued" && state.state !== "generating" && (
        <section className={styles.resetPanel} aria-labelledby="memorial-reset-title">
          <div>
            <p className={commonStyles.eyebrow} lang="en">
              NEW MEMORY
            </p>
            <h2 id="memorial-reset-title">新しい思い出をはじめる</h2>
            <p>親愛度を全てリセットし、最初からやり直します。</p>
          </div>
          <button
            className={commonStyles.secondaryButton}
            type="button"
            disabled={busy || state.state !== "ready" || actionsBlocked}
            onClick={(event) => openConfirmation("reset", event.detail > 0)}
          >
            親愛度をリセット
          </button>
        </section>
      )}

      {dialog?.kind === "generate" && participant && selectedFile && (
        <ConfirmationDialog
          kind="generate"
          participantName={participant.name}
          completionFocusRef={pageHeadingRef}
          pointerInitiated={dialog.pointerInitiated}
          confirmDisabled={actionsBlocked}
          onCancel={cancelConfirmation}
          onConfirm={confirmGeneration}
        />
      )}
      {dialog?.kind === "reset" && state.state === "ready" && (
        <ConfirmationDialog
          kind="reset"
          completionFocusRef={pageHeadingRef}
          pointerInitiated={dialog.pointerInitiated}
          confirmDisabled={actionsBlocked}
          onCancel={cancelConfirmation}
          onConfirm={confirmReset}
        />
      )}
    </div>
  );
}
