import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  defaultRangeExtractor,
  useWindowVirtualizer,
  type VirtualItem,
} from "@tanstack/react-virtual";
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
  galleryCollectionName,
  galleryCollectionSchool,
  galleryDetailQuery,
  galleryImageUrl,
  galleryOriginalQuery,
  galleryRarityAriaLabel,
  galleryThumbnailsQuery,
  isGalleryGif,
  prefersReducedMotion,
  useGalleryReducedMotion,
  type GalleryCollectionEntry,
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

function prefersReducedData() {
  const { connection } = navigator as Navigator & { connection?: { saveData?: boolean } };
  return connection?.saveData || window.matchMedia("(prefers-reduced-data: reduce)").matches;
}

function GalleryHoverPreview({
  cardId,
  scope,
}: {
  readonly cardId: string;
  readonly scope: number;
}) {
  const original = useQuery({
    ...galleryOriginalQuery(scope, cardId),
    retry: false,
    refetchOnReconnect: false,
  });
  const [loadedUrl, setLoadedUrl] = useState<string>();
  const [failedUrl, setFailedUrl] = useState<string>();
  const url = galleryImageUrl(original.data);
  if (!url || failedUrl === url || !isGalleryGif(url)) return null;
  return (
    <img
      alt=""
      className={styles.hoverPreview}
      data-loaded={loadedUrl === url ? "" : undefined}
      decoding="async"
      referrerPolicy="no-referrer"
      src={url}
      onLoad={() => setLoadedUrl(url)}
      onError={() => setFailedUrl(url)}
    />
  );
}

type GalleryGridEntry = GalleryCard | GalleryCollectionEntry;
const hiddenStudentName = "？？？？？";

function entryId(entry: GalleryGridEntry) {
  return "cards" in entry ? entry.studentId : entry.cardId;
}

function entryCard(entry: GalleryGridEntry) {
  return "cards" in entry ? entry.cards[0] : entry;
}

export interface GalleryGridReturn {
  readonly id: string;
  readonly scrollY: number;
  readonly layout: { readonly width: number; readonly top: number };
  readonly measurements: VirtualItem[];
}

function GalleryGridCard({
  entry,
  schoolLogos,
  image,
  thumbnailSettled,
  scope,
  search,
  first,
  previewEnabled,
  onOpen,
  onOpenGroup,
  onFocus,
  onTab,
}: {
  readonly entry: GalleryGridEntry;
  readonly schoolLogos: Readonly<Record<string, string>>;
  readonly image: Thumbnail | undefined;
  readonly thumbnailSettled: boolean;
  readonly scope: number;
  readonly search: string;
  readonly first: boolean;
  readonly previewEnabled: boolean;
  readonly onOpen: (event: MouseEvent<HTMLAnchorElement>) => void;
  readonly onOpenGroup: ((event: MouseEvent<HTMLAnchorElement>, id: string) => void) | undefined;
  readonly onFocus: () => void;
  readonly onTab: (event: KeyboardEvent<HTMLAnchorElement>) => void;
}) {
  const queryClient = useQueryClient();
  const hoverTimer = useRef<number | undefined>(undefined);
  const [previewCard, setPreviewCard] = useState<string>();
  const collection = "cards" in entry ? entry : undefined;
  const card = entryCard(entry);
  const name =
    "cards" in entry ? (galleryCollectionName(entry) ?? hiddenStudentName) : galleryCardName(entry);
  const school = collection ? galleryCollectionSchool(collection) : card?.schoolName;
  const rarity = entry.rarity;
  const copies = collection?.cards.length ?? 0;
  const opensGroup = copies > 1;
  if (previewCard && (!previewEnabled || previewCard !== card?.cardId)) setPreviewCard(undefined);
  const stopHover = () => {
    window.clearTimeout(hoverTimer.current);
    setPreviewCard(undefined);
  };
  const prefetch = () => {
    if (card && !opensGroup) void queryClient.prefetchQuery(galleryDetailQuery(scope, card.cardId));
  };
  useEffect(() => () => window.clearTimeout(hoverTimer.current), [card?.cardId, previewEnabled]);

  const contents = (
    <>
      <div className={styles.cardMedia}>
        {!card ? (
          <span className={styles.question} aria-hidden="true">
            ？
          </span>
        ) : image ? (
          <GalleryThumbnail key={card.cardId} image={image} />
        ) : (
          <span className={styles.imagePlaceholder}>
            {card.thumbnailReady && !thumbnailSettled
              ? "画像を読み込み中"
              : opensGroup
                ? "サムネイルを表示できません"
                : "拡大して画像を見る"}
          </span>
        )}
        {card && previewEnabled && previewCard === card.cardId && (
          <GalleryHoverPreview cardId={card.cardId} scope={scope} />
        )}
        {opensGroup && <span className={styles.copyCount}>{copies.toLocaleString("ja-JP")}枚</span>}
      </div>
      <div className={styles.cardBody}>
        <div className={styles.cardTitle}>
          <strong title={name}>{name}</strong>
          <span className={styles.rarity}>
            <GalleryRarity rarity={rarity} />
          </span>
        </div>
        {school && (
          <span className={styles.school}>
            <GallerySchool schoolName={school} logo={schoolLogos[school]} />
          </span>
        )}
        {card && (
          <>
            <div className={styles.cardFooter}>
              <GalleryCollector card={card} />
              <time dateTime={card.postedAt}>{galleryDateText(card.postedAt)}</time>
            </div>
            {first && <span className={styles.firstCard}>初回獲得</span>}
          </>
        )}
      </div>
    </>
  );

  if (!card)
    return (
      <div className={styles.card} data-rarity={rarity} data-unacquired="">
        {contents}
      </div>
    );

  const id = entryId(entry);
  const params = new URLSearchParams(search);
  if (opensGroup) params.set("student", id);
  return (
    <Link
      aria-label={
        opensGroup
          ? `${name}、${galleryRarityAriaLabel(rarity)}、${copies}枚のカードを見る`
          : `${name}、${galleryRarityAriaLabel(rarity)}を拡大${first ? "、初回獲得" : ""}`
      }
      className={styles.card}
      data-rarity={rarity ?? "unrecorded"}
      to={{
        pathname: opensGroup ? "/gallery" : `/gallery/${card.cardId}`,
        search: params.toString(),
      }}
      state={opensGroup ? { galleryGroupOpened: true } : { galleryOpened: true }}
      onClick={(event) => {
        stopHover();
        if (opensGroup) onOpenGroup?.(event, id);
        else onOpen(event);
      }}
      data-gallery-card={id}
      onFocus={() => {
        onFocus();
        prefetch();
      }}
      onKeyDown={onTab}
      onPointerEnter={(event) => {
        if (
          event.pointerType !== "mouse" ||
          !window.matchMedia("(hover: hover) and (pointer: fine)").matches ||
          prefersReducedData()
        ) {
          return;
        }
        stopHover();
        hoverTimer.current = window.setTimeout(() => {
          prefetch();
          if (previewEnabled && !prefersReducedMotion() && !prefersReducedData()) {
            setPreviewCard(card.cardId);
          }
        }, 130);
      }}
      onPointerLeave={stopHover}
    >
      {contents}
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
  entries,
  schoolLogos,
  scope,
  search,
  label,
  firstCardId,
  viewerOpen,
  restoreFocus,
  onRestoreFocus,
  onOpen,
  onOpenGroup,
}: {
  readonly entries: readonly GalleryGridEntry[];
  readonly schoolLogos: Readonly<Record<string, string>>;
  readonly scope: number;
  readonly search: string;
  readonly label: string;
  readonly firstCardId?: string;
  readonly viewerOpen: boolean;
  readonly restoreFocus?: GalleryGridReturn;
  readonly onRestoreFocus?: () => void;
  readonly onOpen: (event: MouseEvent<HTMLAnchorElement>) => void;
  readonly onOpenGroup?: (
    event: MouseEvent<HTMLAnchorElement>,
    position: GalleryGridReturn,
  ) => void;
}) {
  const grid = useRef<HTMLUListElement>(null);
  const pendingFocus = useRef<{ id: string; scrollY?: number } | undefined>(restoreFocus);
  const [layout, setLayout] = useState(restoreFocus?.layout ?? { width: 0, top: 0 });
  const [focusedCard, setFocusedCard] = useState(restoreFocus?.id);
  const reducedMotion = useGalleryReducedMotion();
  const columns = gridColumns(layout.width);
  const focusedIndex = entries.findIndex((entry) => entryId(entry) === focusedCard);
  const focusedRow = focusedIndex < 0 ? -1 : Math.floor(focusedIndex / columns);
  const collection = entries[0] !== undefined && "cards" in entries[0];
  const firstAcquiredRow = collection ? Math.floor(entries.findIndex(entryCard) / columns) : -1;
  const lastAcquiredRow = collection ? Math.floor(entries.findLastIndex(entryCard) / columns) : -1;
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
    count: Math.ceil(entries.length / columns),
    estimateSize: () => Math.max(210, (layout.width / columns) * 0.462 + 120),
    ...(restoreFocus && {
      initialOffset: restoreFocus.scrollY,
      initialMeasurementsCache: restoreFocus.measurements,
    }),
    scrollMargin: layout.top,
    overscan: 2,
    rangeExtractor: (range) => {
      const rows = defaultRangeExtractor(range);
      // Keep a keyboard entry point when the visible range contains only locked slots.
      for (const row of [focusedRow, firstAcquiredRow, lastAcquiredRow]) {
        if (row >= 0 && !rows.includes(row)) rows.push(row);
      }
      return rows.sort((first, second) => first - second);
    },
  });
  const rows = virtual.getVirtualItems();
  useLayoutEffect(() => {
    const target = pendingFocus.current;
    if (!target) return;
    const link = grid.current?.querySelector<HTMLAnchorElement>(
      `[data-gallery-card="${target.id}"]`,
    );
    if (link) {
      pendingFocus.current = undefined;
      if (target.scrollY !== undefined) window.scrollTo(0, target.scrollY);
      link.focus({ preventScroll: target.scrollY !== undefined });
      if (target.scrollY !== undefined) onRestoreFocus?.();
    }
  });
  const handleTab = (event: KeyboardEvent<HTMLAnchorElement>, index: number) => {
    if (event.key !== "Tab" || event.altKey || event.ctrlKey || event.metaKey) return;
    const direction = event.shiftKey ? -1 : 1;
    let target = index + direction;
    let entry = entries[target];
    while (entry && !entryCard(entry)) {
      target += direction;
      entry = entries[target];
    }
    if (!entry) return;
    const id = entryId(entry);
    if (grid.current?.querySelector(`[data-gallery-card="${id}"]`)) return;
    event.preventDefault();
    pendingFocus.current = { id };
    setFocusedCard(id);
    virtual.scrollToIndex(Math.floor(target / columns), { align: "auto" });
  };
  const batches = useMemo(() => {
    const result: string[][] = [];
    for (let index = 0; index < entries.length; index += galleryThumbnailBatchSize) {
      result.push(
        entries
          .slice(index, index + galleryThumbnailBatchSize)
          .map(entryCard)
          .filter((card) => card?.thumbnailReady)
          .map((card) => card.cardId)
          .sort(),
      );
    }
    return result;
  }, [entries]);
  const activeBatches = new Set<number>();
  for (const row of rows) {
    const firstCard = row.index * columns;
    activeBatches.add(Math.floor(firstCard / galleryThumbnailBatchSize));
    activeBatches.add(
      Math.floor(Math.min(firstCard + columns - 1, entries.length - 1) / galleryThumbnailBatchSize),
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
        aria-label={label}
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
            {entries.slice(row.index * columns, (row.index + 1) * columns).map((entry, offset) => {
              const card = entryCard(entry);
              const id = entryId(entry);
              return (
                <article
                  aria-label={
                    "cards" in entry && !card
                      ? `${galleryCollectionName(entry) ?? hiddenStudentName}、${galleryRarityAriaLabel(entry.rarity)}、未入手`
                      : undefined
                  }
                  aria-posinset={row.index * columns + offset + 1}
                  aria-setsize={entries.length}
                  key={id}
                  // oxlint-disable-next-line jsx-a11y/prefer-tag-over-role -- A virtual row groups the native list items.
                  role="listitem"
                >
                  <GalleryGridCard
                    entry={entry}
                    schoolLogos={schoolLogos}
                    image={card ? thumbnails.get(card.cardId) : undefined}
                    thumbnailSettled={card ? settled.has(card.cardId) : false}
                    scope={scope}
                    search={search}
                    first={firstCardId !== undefined && firstCardId === card?.cardId}
                    previewEnabled={!viewerOpen && !reducedMotion}
                    onOpen={onOpen}
                    onOpenGroup={
                      onOpenGroup &&
                      ((event, id) =>
                        onOpenGroup(event, {
                          id,
                          scrollY: window.scrollY,
                          layout,
                          measurements: virtual.takeSnapshot(),
                        }))
                    }
                    onFocus={() => setFocusedCard(id)}
                    onTab={(event) => handleTab(event, row.index * columns + offset)}
                  />
                </article>
              );
            })}
          </li>
        ))}
      </ul>
    </>
  );
}
