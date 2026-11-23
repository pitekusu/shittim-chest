import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";
import { useSyncExternalStore } from "react";

import {
  getGalleryDetail,
  getGalleryList,
  getGalleryOriginal,
  getGalleryThumbnails,
  type GalleryCard,
  type GalleryCollectionCard,
  type GalleryImageTicket,
  type GalleryRosterEntry,
} from "../api/gallery";

export const galleryListQuery = (scope: number) =>
  infiniteQueryOptions({
    queryKey: ["gallery", scope, "list"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => getGalleryList(pageParam, signal),
    getNextPageParam: ({ nextCursor }, _pages, _currentCursor, cursors) =>
      nextCursor !== null && !cursors.includes(nextCursor) ? nextCursor : undefined,
  });

export interface GalleryCollectionEntry extends GalleryRosterEntry {
  readonly cards: readonly GalleryCard[];
  readonly firstAcquisitionOrder: number | null;
}

export function galleryCollection(
  roster: readonly GalleryRosterEntry[],
  newestFirst: readonly GalleryCollectionCard[],
): GalleryCollectionEntry[] {
  const cardsByStudent = new Map<string, { cards: GalleryCard[]; firstAcquisitionOrder: number }>();
  // The API preserves the exact storage order, including sub-millisecond timestamps.
  for (const card of [...newestFirst].reverse()) {
    const group = cardsByStudent.getOrInsertComputed(card.studentId, () => ({
      cards: [],
      firstAcquisitionOrder: cardsByStudent.size,
    }));
    group.cards.push(card);
  }
  return roster.map((entry) => ({
    ...entry,
    ...(cardsByStudent.get(entry.studentId) ?? { cards: [], firstAcquisitionOrder: null }),
  }));
}

export function galleryCollectionName(entry: GalleryCollectionEntry) {
  return entry.studentName ?? entry.cards[0]?.studentName ?? null;
}

export function galleryCollectionSchool(entry: GalleryCollectionEntry) {
  return entry.schoolName ?? entry.cards[0]?.schoolName ?? null;
}

export function galleryDetailQuery(scope: number, cardId: string) {
  return queryOptions({
    queryKey: ["gallery", scope, "detail", cardId],
    queryFn: ({ signal }) => getGalleryDetail(cardId, signal),
  });
}

function ticketStaleTime(updatedAt: number, items: readonly GalleryImageTicket[]) {
  return items.reduce(
    (duration, { expiresAt }) =>
      Math.min(duration, Math.max(0, Date.parse(expiresAt) - updatedAt - 5_000)),
    60_000,
  );
}

export function galleryThumbnailsQuery(scope: number, cardIds: readonly string[]) {
  return queryOptions({
    queryKey: ["gallery", scope, "thumbnails", ...cardIds],
    queryFn: ({ signal }) => getGalleryThumbnails(cardIds, signal),
    staleTime: ({ state }) => ticketStaleTime(state.dataUpdatedAt, state.data?.items ?? []),
    refetchOnWindowFocus: false,
  });
}

export function galleryOriginalQuery(scope: number, cardId: string) {
  return queryOptions({
    queryKey: ["gallery", scope, "original", cardId],
    queryFn: ({ signal }) => getGalleryOriginal(cardId, signal),
    staleTime: ({ state }) => ticketStaleTime(state.dataUpdatedAt, state.data ? [state.data] : []),
    refetchOnWindowFocus: false,
  });
}

export function galleryImageUrl(ticket: GalleryImageTicket | undefined): string | undefined {
  return ticket && Date.parse(ticket.expiresAt) > Date.now() ? ticket.url : undefined;
}

export function isGalleryAnimation(url: string) {
  try {
    const pathname = new URL(url, window.location.origin).pathname;
    return pathname.endsWith(".gif") || pathname.endsWith(".apng");
  } catch {
    return false;
  }
}

const reducedMotionQuery = "(prefers-reduced-motion: reduce)";

export function prefersReducedMotion() {
  return window.matchMedia(reducedMotionQuery).matches;
}

function subscribeMotion(callback: () => void) {
  const media = window.matchMedia(reducedMotionQuery);
  media.addEventListener("change", callback);
  return () => media.removeEventListener("change", callback);
}

export function useGalleryReducedMotion() {
  return useSyncExternalStore(subscribeMotion, prefersReducedMotion);
}

export const galleryPerformers = {
  "participant-a": "アロナ",
  "participant-b": "プラナ",
  "participant-c": "安倍晋三",
} as const;

export function galleryCardName(card: GalleryCard) {
  const [student, ...variants] = card.studentName
    .replaceAll("(", "（")
    .replaceAll(")", "）")
    .split("（");
  const suffix = variants.map((variant) => `（${variant}`).join("");
  return `${galleryPerformers[card.performer]}（${student}）${suffix}`;
}

export function galleryRarityLabel(rarity: GalleryCard["rarity"]) {
  if (rarity === null) return "未記録";
  return rarity === 0 ? "星なし" : "★".repeat(rarity);
}

export function galleryRarityAriaLabel(rarity: GalleryCard["rarity"]) {
  return rarity === null ? "未記録" : `星${rarity}つ`;
}

export function galleryCollectorName(card: GalleryCard) {
  return card.collector?.displayName ?? (card.isMine ? "自分" : "不明");
}

export const gallerySorts = {
  commonest: "レア度が低い順",
  rarest: "レア度が高い順",
  newest: "初回獲得日が新しい順",
  oldest: "初回獲得日が古い順",
} as const;

function searchStudentName(name: string) {
  return name.normalize("NFKC").trim().toLocaleLowerCase("ja");
}

export function filterGallery(entries: readonly GalleryCollectionEntry[], search: URLSearchParams) {
  const terms = searchStudentName(search.get("q") ?? "")
    .split(/\s+/u)
    .filter(Boolean);
  const rarity = search.get("rarity");
  const school = search.get("school");
  const acquiredOnly = search.get("acquired") === "1";
  const sort = search.get("sort");
  return entries
    .filter((entry) => {
      const name = searchStudentName(galleryCollectionName(entry) ?? "");
      return (
        (!acquiredOnly || entry.cards.length > 0) &&
        terms.every((term) => name.includes(term)) &&
        (!rarity || String(entry.rarity) === rarity) &&
        (!school || galleryCollectionSchool(entry) === school)
      );
    })
    .sort((left, right) => {
      if (sort === "oldest" || sort === "newest") {
        const leftOrder = left.firstAcquisitionOrder;
        const rightOrder = right.firstAcquisitionOrder;
        if (leftOrder === null || rightOrder === null) {
          return Number(leftOrder === null) - Number(rightOrder === null);
        }
        return sort === "oldest" ? leftOrder - rightOrder : rightOrder - leftOrder;
      }
      const starlessLast = Number(left.rarity === 0) - Number(right.rarity === 0);
      return (
        starlessLast ||
        (sort === "rarest" ? right.rarity - left.rarity : left.rarity - right.rarity)
      );
    });
}
