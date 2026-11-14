import { useQueries, useQueryClient } from "@tanstack/react-query";
import { defaultRangeExtractor, useWindowVirtualizer } from "@tanstack/react-virtual";
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { Link } from "react-router-dom";

import {
  galleryThumbnailBatchSize,
  type GalleryCard,
  type GalleryImageTicket,
} from "../api/gallery";
import { RecordsApiError } from "../api/http";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import {
  galleryCardName,
  galleryDetailQuery,
  galleryImageUrl,
  galleryRarityAriaLabel,
  galleryThumbnailsQuery,
} from "../lib/gallery";
import styles from "../styles/gallery.module.css";
import { GalleryCollector, GalleryRarity, GallerySchool } from "./GalleryMetadata";

const galleryDate = new Intl.DateTimeFormat("ja-JP", {
  dateStyle: "medium",
  timeZone: "Asia/Tokyo",
});

export function galleryDateText(postedAt: string) {
  return galleryDate.format(new Date(postedAt));
}

interface Thumbnail {
  readonly ticket: GalleryImageTicket;
  readonly refresh: () => void;
}

function GalleryThumbnail({ image }: { readonly image: Thumbnail }) {
  const retries = useRef(false);
  const [loadedUrl, setLoadedUrl] = useState<string>();
  const [failedUrl, setFailedUrl] = useState<string>();
  const url = loadedUrl ?? galleryImageUrl(image.ticket);
  if (!url) return <span className={styles.imagePlaceholder}>画像を読み込み中</span>;
  return failedUrl === url ? (
    <span className={styles.imagePlaceholder}>サムネイルを表示できません</span>
  ) : (
    <img
      alt=""
      decoding="async"
      loading="lazy"
      referrerPolicy="no-referrer"
      src={url}
      onLoad={() => setLoadedUrl(url)}
      onError={() => {
        setFailedUrl(url);
        setLoadedUrl(undefined);
        if (retries.current) return;
        retries.current = true;
        image.refresh();
      }}
    />
  );
}

function GalleryGridCard({
  card,
  schoolLogo,
  image,
  thumbnailSettled,
  scope,
  search,
  onOpen,
  onFocus,
  onTab,
}: {
  readonly card: GalleryCard;
  readonly schoolLogo: string | undefined;
  readonly image: Thumbnail | undefined;
  readonly thumbnailSettled: boolean;
  readonly scope: number;
  readonly search: string;
  readonly onOpen: (event: MouseEvent<HTMLAnchorElement>, card: GalleryCard) => void;
  readonly onFocus: () => void;
  readonly onTab: (event: KeyboardEvent<HTMLAnchorElement>) => void;
}) {
  const queryClient = useQueryClient();
  const hoverTimer = useRef<number | undefined>(undefined);
  const name = galleryCardName(card);
  const stopHover = () => window.clearTimeout(hoverTimer.current);
  const prefetch = () => void queryClient.prefetchQuery(galleryDetailQuery(scope, card.cardId));
  useEffect(() => () => window.clearTimeout(hoverTimer.current), []);
  return (
    <Link
      aria-label={`${name}、${galleryRarityAriaLabel(card.rarity)}を拡大`}
      className={styles.card}
      data-rarity={card.rarity ?? "unrecorded"}
      to={{ pathname: `/gallery/${card.cardId}`, search }}
      onClick={(event) => onOpen(event, card)}
      data-gallery-card={card.cardId}
      onFocus={() => {
        onFocus();
        prefetch();
      }}
      onKeyDown={onTab}
      onMouseEnter={() => {
        if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
        if (window.matchMedia("(prefers-reduced-data: reduce)").matches) return;
        stopHover();
        hoverTimer.current = window.setTimeout(prefetch, 130);
      }}
      onMouseLeave={stopHover}
    >
      <div className={styles.cardMedia}>
        {image ? (
          <GalleryThumbnail image={image} />
        ) : (
          <span className={styles.imagePlaceholder}>
            {card.thumbnailReady && !thumbnailSettled ? "画像を読み込み中" : "拡大して画像を見る"}
          </span>
        )}
      </div>
      <div className={styles.cardBody}>
        <div className={styles.cardTitle}>
          <strong title={name}>{name}</strong>
          <span className={styles.rarity}>
            <GalleryRarity rarity={card.rarity} />
          </span>
        </div>
        <span className={styles.school}>
          <GallerySchool schoolName={card.schoolName} logo={schoolLogo} />
        </span>
        <div className={styles.cardFooter}>
          <GalleryCollector card={card} />
          <time dateTime={card.postedAt}>{galleryDateText(card.postedAt)}</time>
        </div>
      </div>
    </Link>
  );
}

function gridColumns(width: number) {
  if (width >= 1220) return 4;
  if (width >= 870) return 3;
  if (width >= 550) return 2;
  return 1;
}

export function GalleryGrid({
  cards,
  schoolLogos,
  scope,
  search,
  onOpen,
}: {
  readonly cards: readonly GalleryCard[];
  readonly schoolLogos: Readonly<Record<string, string>>;
  readonly scope: number;
  readonly search: string;
  readonly onOpen: (event: MouseEvent<HTMLAnchorElement>, card: GalleryCard) => void;
}) {
  const grid = useRef<HTMLUListElement>(null);
  const pendingFocus = useRef<string>(undefined);
  const [layout, setLayout] = useState({ width: 0, top: 0 });
  const [focusedCard, setFocusedCard] = useState<string>();
  const columns = gridColumns(layout.width);
  const focusedIndex = cards.findIndex((card) => card.cardId === focusedCard);
  const focusedRow = focusedIndex < 0 ? -1 : Math.floor(focusedIndex / columns);
  useLayoutEffect(() => {
    const element = grid.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      const next = { width: Math.round(rect.width), top: Math.round(rect.top + window.scrollY) };
      setLayout((previous) =>
        previous.width === next.width && previous.top === next.top ? previous : next,
      );
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    if (element.parentElement) observer.observe(element.parentElement);
    window.addEventListener("resize", measure);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", measure);
    };
  }, []);
  // oxlint-disable-next-line react/incompatible-library -- The virtualizer stays inside this un-memoized component.
  const virtual = useWindowVirtualizer({
    count: Math.ceil(cards.length / columns),
    estimateSize: () => Math.max(210, (layout.width / columns) * 0.462 + 120),
    scrollMargin: layout.top,
    overscan: 2,
    rangeExtractor: (range) => {
      const rows = defaultRangeExtractor(range);
      return focusedRow < 0 || rows.includes(focusedRow)
        ? rows
        : [...rows, focusedRow].sort((first, second) => first - second);
    },
  });
  const rows = virtual.getVirtualItems();
  useLayoutEffect(() => {
    const id = pendingFocus.current;
    const link = id
      ? grid.current?.querySelector<HTMLAnchorElement>(`[data-gallery-card="${id}"]`)
      : null;
    if (link) {
      pendingFocus.current = undefined;
      link.focus();
    }
  });
  const handleTab = (event: KeyboardEvent<HTMLAnchorElement>, index: number) => {
    if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = index + (event.shiftKey ? -1 : 1);
    const card = cards[target];
    if (!card || grid.current?.querySelector(`[data-gallery-card="${card.cardId}"]`)) return;
    event.preventDefault();
    pendingFocus.current = card.cardId;
    setFocusedCard(card.cardId);
    virtual.scrollToIndex(Math.floor(target / columns), { align: "auto" });
  };
  const batches = useMemo(() => {
    const result: string[][] = [];
    for (let index = 0; index < cards.length; index += galleryThumbnailBatchSize) {
      result.push(
        cards
          .slice(index, index + galleryThumbnailBatchSize)
          .filter((card) => card.thumbnailReady)
          .map((card) => card.cardId)
          .sort(),
      );
    }
    return result;
  }, [cards]);
  const activeBatches = new Set<number>();
  for (const row of rows) {
    const firstCard = row.index * columns;
    activeBatches.add(Math.floor(firstCard / galleryThumbnailBatchSize));
    activeBatches.add(
      Math.floor(Math.min(firstCard + columns - 1, cards.length - 1) / galleryThumbnailBatchSize),
    );
  }
  const requestedBatches = [...activeBatches]
    .map((batchIndex) => batches[batchIndex] ?? [])
    .filter((batch) => batch.length > 0);
  const thumbnailQueries = useQueries({
    queries: requestedBatches.map((batch) => galleryThumbnailsQuery(scope, batch)),
  });
  const authenticationError = thumbnailQueries.find(
    ({ error }) => error instanceof RecordsApiError && error.status === 401,
  )?.error;
  useAuthenticationRecovery(authenticationError);
  const thumbnails = new Map<string, Thumbnail>();
  const settled = new Set<string>();
  for (const [index, query] of thumbnailQueries.entries()) {
    if (query.isSuccess || query.isError) {
      for (const id of requestedBatches[index] ?? []) settled.add(id);
    }
    for (const ticket of query.data?.items ?? []) {
      thumbnails.set(ticket.cardId, { ticket, refresh: () => void query.refetch() });
    }
  }
  const unavailable = thumbnailQueries.some((query) => query.isError);
  return (
    <>
      {unavailable && (
        <button
          className={styles.secondary}
          type="button"
          onClick={() => {
            for (const query of thumbnailQueries) if (query.isError) void query.refetch();
          }}
        >
          サムネイルを再読み込み
        </button>
      )}
      <ul
        aria-label="獲得した生徒カード"
        className={styles.grid}
        ref={grid}
        style={{ height: virtual.getTotalSize() }}
      >
        {rows.map((row) => (
          <li
            className={styles.row}
            data-index={row.index}
            key={row.key}
            ref={virtual.measureElement}
            role="none"
            style={{
              gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
              transform: `translateY(${row.start - layout.top}px)`,
            }}
          >
            {cards.slice(row.index * columns, (row.index + 1) * columns).map((card, offset) => (
              <article
                aria-posinset={row.index * columns + offset + 1}
                aria-setsize={cards.length}
                key={card.cardId}
                // oxlint-disable-next-line jsx-a11y/prefer-tag-over-role -- A virtual row groups the native list items.
                role="listitem"
              >
                <GalleryGridCard
                  card={card}
                  schoolLogo={schoolLogos[card.schoolName]}
                  image={thumbnails.get(card.cardId)}
                  thumbnailSettled={settled.has(card.cardId)}
                  scope={scope}
                  search={search}
                  onOpen={onOpen}
                  onFocus={() => setFocusedCard(card.cardId)}
                  onTab={(event) => handleTab(event, row.index * columns + offset)}
                />
              </article>
            ))}
          </li>
        ))}
      </ul>
    </>
  );
}
