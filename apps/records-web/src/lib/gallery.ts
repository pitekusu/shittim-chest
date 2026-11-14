import { infiniteQueryOptions, queryOptions } from "@tanstack/react-query";

import {
  getGalleryDetail,
  getGalleryList,
  getGalleryOriginal,
  getGalleryThumbnails,
  type GalleryCard,
  type GalleryImageTicket,
} from "../api/gallery";

export const galleryListQuery = (scope: number) =>
  infiniteQueryOptions({
    queryKey: ["gallery", scope, "list"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => getGalleryList(pageParam, signal),
    getNextPageParam: ({ nextCursor }, _pages, _currentCursor, cursors) =>
      nextCursor !== null && !cursors.includes(nextCursor) ? nextCursor : undefined,
  });

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
  newest: "獲得日が新しい順",
  oldest: "獲得日が古い順",
  rarest: "レア度が高い順",
  commonest: "レア度が低い順",
  school: "学校名順",
  collector: "獲得者名順",
} as const;

const japanese = new Intl.Collator("ja");

export function filterGallery(cards: readonly GalleryCard[], search: URLSearchParams) {
  const collector = search.get("collector");
  const performer = search.get("performer");
  const rarity = search.get("rarity");
  const school = search.get("school");
  const sort = search.get("sort");
  return cards
    .filter((card) => {
      const matchesCollector =
        collector === "mine"
          ? card.isMine
          : collector === "unknown"
            ? card.collector === null && !card.isMine
            : !collector || card.collector?.id === collector;
      return (
        matchesCollector &&
        (!performer || card.performer === performer) &&
        (!rarity || String(card.rarity ?? "unrecorded") === rarity) &&
        (!school || card.schoolName === school)
      );
    })
    .sort((left, right) => {
      const byDate = Date.parse(right.postedAt) - Date.parse(left.postedAt);
      const byRarity = (right.rarity ?? 0) - (left.rarity ?? 0);
      const unknownRarity = Number(left.rarity === null) - Number(right.rarity === null);
      const bySchool = japanese.compare(left.schoolName, right.schoolName);
      const byCollector = japanese.compare(
        left.collector?.displayName ?? "",
        right.collector?.displayName ?? "",
      );
      const primary =
        sort === "oldest"
          ? -byDate
          : sort === "rarest"
            ? unknownRarity || byRarity
            : sort === "commonest"
              ? unknownRarity || -byRarity
              : sort === "school"
                ? bySchool
                : sort === "collector"
                  ? byCollector
                  : byDate;
      return primary || byDate || left.cardId.localeCompare(right.cardId);
    });
}
