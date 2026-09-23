import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import {
  getMomotalkRoom,
  getMomotalkRooms,
  getMomotalkWeeks,
  type MomotalkImage,
  type MomotalkMessage,
  type MomotalkRoomResponse,
  type MomotalkWeek,
} from "../api/momotalk";
import { Avatar } from "../components/Avatar";
import { ErrorPanel } from "../components/ErrorPanel";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import common from "../styles/common.module.css";
import styles from "../styles/momotalk.module.css";

const STATES = { preparing: "準備中", ready: "会話を見る", failed: "生成できませんでした" };
const weekDate = new Intl.DateTimeFormat("ja-JP", {
  timeZone: "Asia/Tokyo",
  year: "numeric",
  month: "long",
  day: "numeric",
});
const graphemes = new Intl.Segmenter("ja", { granularity: "grapheme" });

function WeekPicker({
  weeks,
  value,
  onChange,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  readonly weeks: readonly MomotalkWeek[];
  readonly value: string;
  readonly onChange: (weekId: string) => void;
  readonly hasMore: boolean;
  readonly loadingMore: boolean;
  readonly onLoadMore: () => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const options = useRef<(HTMLButtonElement | null)[]>([]);
  const selectedIndex = Math.max(
    0,
    weeks.findIndex((week) => week.weekId === value),
  );
  const selected = weeks[selectedIndex];

  useEffect(() => {
    if (!open) return;
    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnOutsideFocus = (event: FocusEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape" && root.current?.contains(document.activeElement)) {
        event.preventDefault();
        closeAndFocus();
      }
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("focusin", closeOnOutsideFocus);
    document.addEventListener("keydown", closeOnEscape);
    options.current[selectedIndex]?.focus();
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("focusin", closeOnOutsideFocus);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open, selectedIndex]);

  function closeAndFocus() {
    setOpen(false);
    trigger.current?.focus();
  }

  function moveOption(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next: number | undefined;
    if (event.key === "ArrowDown") next = Math.min(index + 1, weeks.length - 1);
    if (event.key === "ArrowUp") next = Math.max(index - 1, 0);
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = weeks.length - 1;
    if (next === undefined) return;
    event.preventDefault();
    options.current[next]?.focus();
  }

  return (
    <div ref={root} className={styles.weekPicker}>
      <span id="momotalk-week-label">今週とこれまでの会話</span>
      <button
        ref={trigger}
        className={styles.weekTrigger}
        type="button"
        aria-labelledby="momotalk-week-label momotalk-week-value"
        aria-expanded={open}
        aria-controls="momotalk-week-menu"
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (!open && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault();
            setOpen(true);
          }
        }}
      >
        <span className={styles.weekTriggerIcon} aria-hidden="true">
          <svg
            viewBox="0 0 24 24"
            width="18"
            height="18"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <rect x="3" y="5" width="18" height="16" rx="2" />
            <path d="M7 3v4m10-4v4M3 10h18" />
          </svg>
        </span>
        <span id="momotalk-week-value" className={styles.weekTriggerValue}>
          {selected ? `${weekDate.format(new Date(selected.publishAt))}の週` : "週を選択"}
        </span>
        <span className={styles.weekChevron} aria-hidden="true">
          ⌄
        </span>
      </button>
      {open && (
        <div id="momotalk-week-menu" className={styles.weekMenu}>
          <div className={styles.weekMenuHeading}>会話の週を選択</div>
          <div className={styles.weekOptions}>
            {weeks.map((week, index) => (
              <button
                key={week.weekId}
                ref={(node) => {
                  options.current[index] = node;
                }}
                type="button"
                className={styles.weekOption}
                aria-current={week.weekId === value ? "true" : undefined}
                onKeyDown={(event) => moveOption(event, index)}
                onClick={() => {
                  onChange(week.weekId);
                  closeAndFocus();
                }}
              >
                <span className={styles.weekOptionDate}>
                  {weekDate.format(new Date(week.publishAt))}の週
                </span>
                {index === 0 && <span className={styles.weekLatest}>最新</span>}
                {week.weekId === value && (
                  <span className={styles.weekCheck} aria-hidden="true">
                    ✓
                  </span>
                )}
              </button>
            ))}
          </div>
          {hasMore && (
            <button
              type="button"
              className={styles.weekLoadMore}
              disabled={loadingMore}
              onClick={onLoadMore}
            >
              {loadingMore ? "読み込み中…" : "以前の週を読み込む"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function usePlayback(messages: readonly MomotalkMessage[]) {
  const [completed, setCompleted] = useState(0);
  const [letters, setLetters] = useState(0);
  const [skipped, setSkipped] = useState(false);
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  const [reduced, setReduced] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onVisibility = () => setVisible(document.visibilityState !== "hidden");
    const onPreference = () => setReduced(preference.matches);
    document.addEventListener("visibilitychange", onVisibility);
    preference.addEventListener("change", onPreference);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      preference.removeEventListener("change", onPreference);
    };
  }, []);

  const current = messages[completed];
  const units = current
    ? Array.from(graphemes.segment(current.text), ({ segment }) => segment)
    : [];
  useEffect(() => {
    if (skipped || reduced || !visible || !current) return;
    const timeout = window.setTimeout(
      () => {
        if (letters < units.length) setLetters(letters + 1);
        else {
          setCompleted(completed + 1);
          setLetters(0);
        }
      },
      letters === 0 ? 700 : letters === units.length ? 600 : 35,
    );
    return () => window.clearTimeout(timeout);
  }, [completed, current, letters, reduced, skipped, units.length, visible]);

  const shown = skipped || reduced ? messages.length : completed;
  return {
    shown,
    partial: units.slice(0, letters).join(""),
    done: shown >= messages.length,
    skip: () => setSkipped(true),
    replay: () => {
      setCompleted(0);
      setLetters(0);
      setSkipped(false);
    },
  };
}

function ImageViewer({
  image,
  onClose,
}: {
  readonly image: MomotalkImage;
  readonly onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    element?.showModal();
    return () => {
      element?.close();
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className={styles.imageDialog}
      aria-label="モモトークの画像"
      onCancel={onClose}
    >
      <div className={styles.imageActions}>
        <a className={common.primaryButton} href={image.downloadUrl ?? undefined} rel="noreferrer">
          画像を保存
        </a>
        <button type="button" onClick={onClose} autoFocus>
          閉じる
        </button>
      </div>
      <img
        src={image.url ?? undefined}
        alt="AIが生成したモモトークの自撮り画像"
        referrerPolicy="no-referrer"
      />
    </dialog>
  );
}

function Conversation({
  data,
  onBack,
}: {
  readonly data: MomotalkRoomResponse;
  readonly onBack: () => void;
}) {
  const playback = usePlayback(data.messages);
  const transcript = useRef<HTMLDivElement>(null);
  const followsBottom = useRef(true);
  const [awayFromBottom, setAwayFromBottom] = useState(false);
  const [imageMood, setImageMood] = useState<MomotalkImage["mood"] | null>(null);
  const image = data.images.find((candidate) => candidate.mood === imageMood);

  useEffect(() => {
    if (followsBottom.current && transcript.current) {
      transcript.current.scrollTop = transcript.current.scrollHeight;
    }
  }, [playback.partial, playback.shown, data.images]);

  function toBottom() {
    followsBottom.current = true;
    setAwayFromBottom(false);
    if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  }
  const current = data.messages[playback.shown];
  const currentSpeaker = data.participants.find(
    (participant) => participant.slot === current?.participant,
  );

  return (
    <section
      className={styles.conversation}
      aria-label={`${data.room.requester.displayName}についての会話`}
    >
      <header className={styles.roomHeader}>
        <button type="button" className={styles.back} onClick={onBack}>
          一覧へ戻る
        </button>
        <Avatar avatar={data.room.requester.avatar} />
        <div>
          <h2>{data.room.requester.displayName}</h2>
          <p>この週の質問 {data.room.questionCount}回</p>
        </div>
      </header>
      <div className={styles.playbackBar}>
        <span>週ごとに届く3人の会話を再生しています</span>
        <button
          type="button"
          onClick={() => {
            if (playback.done) playback.replay();
            else playback.skip();
            toBottom();
          }}
        >
          {playback.done ? "もう一度再生" : "すべて表示"}
        </button>
      </div>
      <div
        ref={transcript}
        className={styles.transcript}
        onScroll={() => {
          const element = transcript.current;
          if (!element) return;
          const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 70;
          followsBottom.current = atBottom;
          setAwayFromBottom(!atBottom);
        }}
      >
        <p className={styles.dateChip}>
          {weekDate.format(new Date(data.week.publishAt))}のモモトーク
        </p>
        {data.room.state !== "ready" && (
          <output className={styles.empty}>
            {data.room.state === "preparing"
              ? "3人が話す準備をしています。完成するとここに届きます。"
              : "今週の会話を生成できませんでした。過去の週の会話は引き続き読めます。"}
          </output>
        )}
        <ol className={styles.messages} aria-label="チャットの発言">
          {data.messages.slice(0, playback.shown).map((message) => {
            const participant = data.participants.find((item) => item.slot === message.participant);
            if (!participant) return null;
            return (
              <li
                key={message.id}
                className={styles.message}
                data-participant={message.participant}
              >
                <Avatar avatar={participant.avatar} />
                <div>
                  <strong>{participant.displayName}</strong>
                  <p className={styles.bubble}>{message.text}</p>
                </div>
              </li>
            );
          })}
          {!playback.done && currentSpeaker && (
            <li
              className={styles.message}
              data-participant={currentSpeaker.slot}
              aria-hidden="true"
            >
              <Avatar avatar={currentSpeaker.avatar} />
              <div>
                <strong>{currentSpeaker.displayName}</strong>
                <p className={styles.bubble}>
                  {playback.partial || (
                    <span className={styles.typing}>
                      <i />
                      <i />
                      <i />
                    </span>
                  )}
                </p>
              </div>
            </li>
          )}
          {playback.done &&
            data.images.map((item) => {
              const participant = data.participants.find(
                (candidate) => candidate.slot === item.participant,
              );
              if (!participant) return null;
              return (
                <li key={item.mood} className={styles.message} data-participant={item.participant}>
                  <Avatar avatar={participant.avatar} />
                  <div>
                    <strong>{participant.displayName}</strong>
                    {item.state === "ready" && item.thumbnailUrl ? (
                      <button
                        className={styles.thumbnail}
                        type="button"
                        onClick={() => setImageMood(item.mood)}
                        aria-label={`${participant.displayName}の画像を拡大`}
                      >
                        <img
                          src={item.thumbnailUrl}
                          alt={`${participant.displayName}の自撮り（AI生成）`}
                          referrerPolicy="no-referrer"
                          loading="lazy"
                          width={320}
                          height={480}
                          onLoad={() => {
                            if (followsBottom.current) toBottom();
                          }}
                        />
                        <span>タップして拡大・保存</span>
                      </button>
                    ) : (
                      <p className={styles.bubble}>
                        {item.state === "pending"
                          ? "画像を準備しています"
                          : "画像を生成できませんでした"}
                      </p>
                    )}
                  </div>
                </li>
              );
            })}
        </ol>
        <p className={common.visuallyHidden} aria-live="polite" aria-atomic="true">
          {playback.shown > 0
            ? `${data.participants.find((p) => p.slot === data.messages[playback.shown - 1]?.participant)?.displayName}: ${data.messages[playback.shown - 1]?.text}`
            : "会話の再生を開始しました"}
        </p>
      </div>
      {awayFromBottom && (
        <button className={styles.toBottom} type="button" onClick={toBottom}>
          会話の続きへ
        </button>
      )}
      {image?.state === "ready" && <ImageViewer image={image} onClose={() => setImageMood(null)} />}
    </section>
  );
}

export default function MomotalkPage() {
  const [chosenWeek, setChosenWeek] = useState<string | null>(null);
  const [roomId, setRoomId] = useState<string | null>(null);
  const weeksQuery = useInfiniteQuery({
    queryKey: ["momotalk", "weeks"],
    queryFn: ({ pageParam }) => getMomotalkWeeks(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    refetchInterval: 60_000,
  });
  const weeks = weeksQuery.data?.pages.flatMap((page) => page.weeks) ?? [];
  const weekId = chosenWeek ?? weeks[0]?.weekId;
  const roomsQuery = useInfiniteQuery({
    queryKey: ["momotalk", "rooms", weekId],
    enabled: Boolean(weekId),
    queryFn: ({ pageParam }) => getMomotalkRooms(weekId ?? "", pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.nextCursor ?? undefined,
    refetchInterval: 60_000,
  });
  const rooms = roomsQuery.data?.pages.flatMap((page) => page.rooms) ?? [];
  const roomQuery = useQuery({
    queryKey: ["momotalk", "room", weekId, roomId],
    enabled: Boolean(weekId && roomId),
    queryFn: () => getMomotalkRoom(weekId ?? "", roomId ?? ""),
    refetchInterval: 60_000,
  });
  const error = weeksQuery.error ?? roomsQuery.error ?? roomQuery.error;
  useAuthenticationRecovery(error);

  return (
    <section className={styles.page}>
      <header className={styles.heading}>
        <div>
          <p className={common.eyebrow} lang="en">
            MOMOTALK
          </p>
          <h1>モモトーク</h1>
        </div>
        {weekId && (
          <WeekPicker
            weeks={weeks}
            value={weekId}
            onChange={(week) => {
              setChosenWeek(week);
              setRoomId(null);
            }}
            hasMore={Boolean(weeksQuery.hasNextPage)}
            loadingMore={weeksQuery.isFetchingNextPage}
            onLoadMore={() => void weeksQuery.fetchNextPage()}
          />
        )}
      </header>
      {error && (
        <ErrorPanel
          title="モモトークを読み込めません"
          message="時間をおいて、もう一度お試しください。"
          onRetry={() => {
            void weeksQuery.refetch();
            if (weekId) void roomsQuery.refetch();
            if (roomId) void roomQuery.refetch();
          }}
        />
      )}
      <div className={styles.workspace} data-room-open={Boolean(roomId)}>
        <aside className={styles.contacts} aria-label="質問者一覧">
          <div className={styles.contactsHeading}>
            <span>CHAT ROOMS</span>
            <span>{rooms.length}</span>
          </div>
          {(weeksQuery.isPending || (weekId && roomsQuery.isPending)) && (
            <output className={styles.empty}>
              会話を読み込んでいます
              <span className={styles.typing}>
                <i />
                <i />
                <i />
              </span>
            </output>
          )}
          {!weeksQuery.isPending && weeks.length === 0 && !error && (
            <p className={styles.empty}>最初のモモトークは日曜日20時に届きます。</p>
          )}
          <ul>
            {rooms.map((room) => (
              <li key={room.roomId}>
                <button
                  type="button"
                  aria-current={roomId === room.roomId ? "true" : undefined}
                  onClick={() => setRoomId(room.roomId)}
                >
                  <Avatar avatar={room.requester.avatar} />
                  <span>
                    <strong>{room.requester.displayName}</strong>
                    <small>{STATES[room.state]}</small>
                  </span>
                  <span aria-hidden="true">›</span>
                </button>
              </li>
            ))}
          </ul>
          {roomsQuery.hasNextPage && (
            <button
              type="button"
              disabled={roomsQuery.isFetchingNextPage}
              onClick={() => void roomsQuery.fetchNextPage()}
            >
              もっと見る
            </button>
          )}
        </aside>
        {roomId && roomQuery.data ? (
          <Conversation
            key={`${weekId}/${roomId}`}
            data={roomQuery.data}
            onBack={() => setRoomId(null)}
          />
        ) : (
          <section className={styles.welcome} aria-label="会話を選択">
            <div className={styles.welcomeMark} aria-hidden="true">
              …
            </div>
            {roomId ? (
              <>
                <button type="button" className={styles.back} onClick={() => setRoomId(null)}>
                  一覧へ戻る
                </button>
                <output>
                  {roomQuery.error ? "会話を取得できませんでした" : "会話を読み込んでいます"}
                </output>
              </>
            ) : (
              <h2>質問者を選択してください。</h2>
            )}
          </section>
        )}
      </div>
    </section>
  );
}
