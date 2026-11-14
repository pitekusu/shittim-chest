import { Dialog } from "@base-ui/react/dialog";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type RefObject } from "react";
import Lightbox, { NextIcon, PreviousIcon, type Slide } from "yet-another-react-lightbox";
import Inline from "yet-another-react-lightbox/plugins/inline";
import Zoom from "yet-another-react-lightbox/plugins/zoom";
import "yet-another-react-lightbox/styles.css";

import { galleryCardIdPattern, type GalleryCard } from "../api/gallery";
import { RecordsApiError } from "../api/http";
import { useAuthenticationRecovery } from "../hooks/useAuthenticationRecovery";
import {
  galleryCardName,
  galleryDetailQuery,
  galleryImageUrl,
  galleryOriginalQuery,
} from "../lib/gallery";
import styles from "../styles/gallery.module.css";
import { galleryDateText } from "./GalleryGrid";
import { GalleryCollector, GalleryRarity, GallerySchool } from "./GalleryMetadata";

declare module "yet-another-react-lightbox" {
  interface SlideTypes {
    "gallery-placeholder": { readonly type: "gallery-placeholder" };
  }
}

const placeholder: Slide = { type: "gallery-placeholder" };
const reducedMotionQuery = "(prefers-reduced-motion: reduce)";
function subscribeMotion(callback: () => void) {
  const media = window.matchMedia(reducedMotionQuery);
  media.addEventListener("change", callback);
  return () => media.removeEventListener("change", callback);
}
function getReducedMotion() {
  return window.matchMedia(reducedMotionQuery).matches;
}

function pointerEdge(stage: HTMLDivElement, x: number, y: number) {
  const stageBounds = stage.getBoundingClientRect();
  const image = stage.querySelector("img");
  // Use the fitted size so zoom and pan leave the navigation at a stable location.
  const width = Math.min(image?.clientWidth ?? stageBounds.width, stageBounds.width);
  const height = Math.min(image?.clientHeight ?? stageBounds.height, stageBounds.height);
  const left = stageBounds.left + (stageBounds.width - width) / 2;
  const right = left + width;
  const top = stageBounds.top + (stageBounds.height - height) / 2;
  if (x < left || x > right || y < top || y > top + height) return undefined;
  const edgeWidth = Math.min(width / 3, Math.max(64, width / 5));
  if (x - left <= edgeWidth) return "left";
  if (right - x <= edgeWidth) return "right";
  return undefined;
}

function ViewerImageError({
  onFailure,
  pending,
  onRetry,
}: {
  readonly onFailure: () => void;
  readonly pending: boolean;
  readonly onRetry: () => void;
}) {
  useEffect(onFailure, [onFailure]);
  return (
    <div className={styles.viewerState}>
      <output>{pending ? "画像を再読み込み中…" : "原寸画像を読み込めませんでした。"}</output>
      {!pending && (
        <button className={styles.viewerButton} type="button" onClick={onRetry}>
          もう一度試す
        </button>
      )}
    </div>
  );
}

function GalleryViewerContent({
  cardId,
  open,
  scope,
  cards,
  schoolLogos,
  onSelect,
}: {
  readonly cardId: string;
  readonly open: boolean;
  readonly scope: number;
  readonly cards: readonly GalleryCard[];
  readonly schoolLogos: Readonly<Record<string, string>>;
  readonly onSelect: (cardId: string) => void;
}) {
  const queryClient = useQueryClient();
  const reducedMotion = useSyncExternalStore(subscribeMotion, getReducedMotion);
  const validId = galleryCardIdPattern.test(cardId);
  const detail = useQuery({ ...galleryDetailQuery(scope, cardId), enabled: open && validId });
  const original = useQuery({
    ...galleryOriginalQuery(scope, cardId),
    enabled: open && validId && detail.isSuccess,
  });
  useAuthenticationRecovery(detail.error);
  useAuthenticationRecovery(original.error);
  const currentIndex = cards.findIndex((item) => item.cardId === cardId);
  const card = detail.data?.card ?? cards[currentIndex];
  const previous = currentIndex < 1 ? undefined : cards[currentIndex - 1]?.cardId;
  const next = currentIndex < 0 ? undefined : cards[currentIndex + 1]?.cardId;
  useEffect(() => {
    if (!open || !detail.isSuccess) return;
    for (const id of [previous, next]) {
      if (id) void queryClient.prefetchQuery(galleryDetailQuery(scope, id));
    }
  }, [detail.isSuccess, next, open, previous, queryClient, scope]);

  const lastFailedCard = useRef<string>(undefined);
  const [displayedImage, setDisplayedImage] = useState<{ cardId: string; url: string }>();
  const [hoveredEdge, setHoveredEdge] = useState<ReturnType<typeof pointerEdge>>();
  const freshUrl = galleryImageUrl(original.data);
  if (freshUrl && (displayedImage?.cardId !== cardId || displayedImage.url !== freshUrl)) {
    setDisplayedImage({ cardId, url: freshUrl });
  }
  // Keep the decoded image visible as its ticket ages while the viewer stays open.
  const imageUrl = freshUrl ?? (displayedImage?.cardId === cardId ? displayedImage.url : undefined);
  const title = card ? galleryCardName(card) : "生徒カード";
  const width = detail.data?.width;
  const height = detail.data?.height;
  const index = previous ? 1 : 0;
  const slides = useMemo(() => {
    const current: Slide =
      imageUrl && width && height ? { src: imageUrl, alt: title, width, height } : placeholder;
    return [...(previous ? [placeholder] : []), current, ...(next ? [placeholder] : [])];
  }, [height, imageUrl, next, previous, title, width]);
  const unavailable =
    !validId || (detail.error instanceof RecordsApiError && detail.error.status === 404);
  const loadError = detail.isError || original.isError;
  const retry = () => {
    if (detail.isError) void detail.refetch();
    else void original.refetch();
  };

  return (
    <>
      <header className={styles.viewerHeader}>
        <div>
          <Dialog.Title className={styles.viewerTitle}>{title}</Dialog.Title>
          {card && (
            <Dialog.Description className={styles.viewerSubtitle}>
              <GallerySchool schoolName={card.schoolName} logo={schoolLogos[card.schoolName]} />
              <GalleryRarity rarity={card.rarity} />
            </Dialog.Description>
          )}
        </div>
        <Dialog.Close
          className={styles.viewerClose}
          aria-label="拡大表示を閉じる"
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft" && previous) onSelect(previous);
            if (event.key === "ArrowRight" && next) onSelect(next);
          }}
        >
          ×
        </Dialog.Close>
      </header>
      {unavailable ? (
        <output className={styles.viewerState}>この生徒カードは見つかりませんでした。</output>
      ) : (
        <div
          className={styles.viewerStage}
          data-hovered-edge={hoveredEdge}
          onPointerMoveCapture={(event) => {
            if (event.pointerType !== "mouse") return;
            setHoveredEdge(
              event.buttons === 0
                ? pointerEdge(event.currentTarget, event.clientX, event.clientY)
                : undefined,
            );
          }}
          onPointerLeave={() => setHoveredEdge(undefined)}
        >
          <Lightbox
            plugins={[Inline, Zoom]}
            inline={{ className: styles.viewerImage }}
            index={index}
            slides={slides}
            carousel={{
              finite: true,
              preload: 0,
              padding: 0,
              imageProps: { referrerPolicy: "no-referrer" },
            }}
            controller={{ closeOnEscape: false }}
            animation={{
              navigation: 0,
              swipe: reducedMotion ? 0 : 160,
              zoom: reducedMotion ? 0 : 140,
            }}
            toolbar={{ buttons: ["zoom"] }}
            labels={{
              Previous: "前のカード",
              Next: "次のカード",
              "Zoom in": "拡大",
              "Zoom out": "縮小",
            }}
            on={{
              view: ({ index: selectedIndex }) => {
                const selected =
                  selectedIndex < index ? previous : selectedIndex > index ? next : undefined;
                if (open && selected) onSelect(selected);
              },
            }}
            render={{
              buttonPrev: () => null,
              buttonNext: () => null,
              slide: ({ slide }) =>
                slide.type === "gallery-placeholder" ? (
                  <div className={styles.viewerState}>
                    <output>
                      {loadError ? "原寸画像を読み込めませんでした。" : "原寸画像を読み込み中…"}
                    </output>
                    {loadError && (
                      <button className={styles.viewerButton} type="button" onClick={retry}>
                        もう一度試す
                      </button>
                    )}
                  </div>
                ) : undefined,
              iconError: () => (
                <ViewerImageError
                  pending={original.isFetching}
                  onRetry={retry}
                  onFailure={() => {
                    if (!open || lastFailedCard.current === cardId) return;
                    lastFailedCard.current = cardId;
                    void original.refetch();
                  }}
                />
              ),
            }}
          />
          {currentIndex >= 0 && cards.length > 1 && (
            <>
              <button
                aria-disabled={!previous}
                aria-label="前のカード"
                className={styles.viewerPrevious}
                type="button"
                onClick={() => {
                  if (previous) onSelect(previous);
                }}
              >
                <PreviousIcon />
              </button>
              <button
                aria-disabled={!next}
                aria-label="次のカード"
                className={styles.viewerNext}
                type="button"
                onClick={() => {
                  if (next) onSelect(next);
                }}
              >
                <NextIcon />
              </button>
            </>
          )}
        </div>
      )}
      {card && (
        <footer className={styles.viewerFooter}>
          <span className={styles.viewerCollector}>
            獲得者 <GalleryCollector card={card} />
          </span>
          <time dateTime={card.postedAt}>{galleryDateText(card.postedAt)}</time>
          {currentIndex >= 0 && (
            <span className={styles.viewerCounter} aria-live="polite">
              {currentIndex + 1} / {cards.length}
            </span>
          )}
        </footer>
      )}
    </>
  );
}

export default function GalleryViewer({
  cardId,
  scope,
  cards,
  schoolLogos,
  onSelect,
  onClose,
  trigger,
  fallbackFocus,
}: {
  readonly cardId: string | undefined;
  readonly scope: number;
  readonly cards: readonly GalleryCard[];
  readonly schoolLogos: Readonly<Record<string, string>>;
  readonly onSelect: (cardId: string) => void;
  readonly onClose: () => void;
  readonly trigger: RefObject<HTMLAnchorElement | null>;
  readonly fallbackFocus: RefObject<HTMLHeadingElement | null>;
}) {
  const [lastCardId, setLastCardId] = useState(cardId);
  if (cardId && lastCardId !== cardId) setLastCardId(cardId);
  const shownCard = cardId ?? lastCardId;
  return (
    <Dialog.Root
      open={cardId !== undefined}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className={styles.backdrop} />
        <Dialog.Viewport className={styles.viewport}>
          <Dialog.Popup
            className={styles.viewer}
            finalFocus={() =>
              trigger.current?.isConnected ? trigger.current : fallbackFocus.current
            }
          >
            {shownCard && (
              <GalleryViewerContent
                cardId={shownCard}
                open={cardId !== undefined}
                scope={scope}
                cards={cards}
                schoolLogos={schoolLogos}
                onSelect={onSelect}
              />
            )}
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
