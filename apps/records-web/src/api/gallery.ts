import detailValidator from "../generated/gallery-detail-response-validator.mjs";
import listValidator from "../generated/gallery-collection-list-response-validator.mjs";
import rosterValidator from "../generated/gallery-roster-response-validator.mjs";
import originalValidator from "../generated/gallery-original-response-validator.mjs";
import schoolLogosValidator from "../generated/gallery-school-logos-response-validator.mjs";
import thumbnailsValidator from "../generated/gallery-thumbnails-response-validator.mjs";
import { requestJson } from "./http";
import type { AvatarRef, ParticipantSlot } from "./types";

export const galleryCardIdPattern = /^[A-Za-z0-9_-]{80}$/u;
export const galleryThumbnailBatchSize = 24;
const galleryEndpoint = "/api/v1/gallery";
const avatarContract = "gallery-avatars-v1";
const collectionContract = "gallery-collection-v1";

export interface GalleryCard {
  readonly cardId: string;
  readonly postedAt: string;
  readonly performer: ParticipantSlot;
  readonly studentName: string;
  readonly schoolName: string;
  readonly rarity: 0 | 1 | 2 | 3 | 4 | null;
  readonly collector: {
    readonly id: string;
    readonly displayName: string;
    readonly avatar?: AvatarRef | null;
  } | null;
  readonly isMine: boolean;
  readonly thumbnailReady: boolean;
}

export interface GalleryCollectionCard extends GalleryCard {
  readonly studentId: string;
}

export interface GalleryRosterEntry {
  readonly studentId: string;
  readonly studentName: string | null;
  readonly schoolName: string | null;
  readonly rarity: NonNullable<GalleryCard["rarity"]>;
}

interface GalleryRosterResponse {
  readonly schemaVersion: 1;
  readonly items: readonly GalleryRosterEntry[];
}

interface GalleryListResponse {
  readonly schemaVersion: 1;
  readonly contract: typeof collectionContract;
  readonly items: readonly GalleryCollectionCard[];
  readonly nextCursor: string | null;
}

export interface GalleryDetailResponse {
  readonly schemaVersion: 1;
  readonly card: GalleryCard;
  readonly width: number;
  readonly height: number;
}

export interface GalleryImageTicket {
  readonly url: string;
  readonly expiresAt: string;
}

interface GalleryThumbnailsResponse {
  readonly schemaVersion: 1;
  readonly items: readonly (GalleryImageTicket & { readonly cardId: string })[];
}

interface GalleryOriginalResponse extends GalleryImageTicket {
  readonly schemaVersion: 1;
}

interface GallerySchoolLogosResponse {
  readonly schemaVersion: 1;
  readonly logos: Readonly<Record<string, string>>;
}

export function getGalleryList(cursor: string | null, signal: AbortSignal) {
  const search = new URLSearchParams({ contract: collectionContract });
  if (cursor !== null) search.set("cursor", cursor);
  return requestJson(
    `${galleryEndpoint}?${search}`,
    (value): value is GalleryListResponse => listValidator(value),
    { signal },
  );
}

export function getGalleryRoster(signal: AbortSignal) {
  return requestJson(
    `${galleryEndpoint}/roster`,
    (value): value is GalleryRosterResponse => rosterValidator(value),
    { signal },
  );
}

export function getGalleryDetail(cardId: string, signal: AbortSignal) {
  return requestJson(
    `${galleryEndpoint}/${encodeURIComponent(cardId)}?contract=${avatarContract}`,
    (value): value is GalleryDetailResponse => detailValidator(value),
    { signal },
  );
}

export function getGallerySchoolLogos(signal: AbortSignal) {
  return requestJson(
    `${galleryEndpoint}/school-logos`,
    (value): value is GallerySchoolLogosResponse => schoolLogosValidator(value),
    { signal },
  );
}

export function getGalleryThumbnails(cardIds: readonly string[], signal: AbortSignal) {
  const search = new URLSearchParams({ ids: cardIds.join(",") });
  return requestJson(
    `${galleryEndpoint}/thumbnails?${search}`,
    (value): value is GalleryThumbnailsResponse => thumbnailsValidator(value),
    { signal },
  );
}

export function getGalleryOriginal(cardId: string, signal: AbortSignal) {
  return requestJson(
    `${galleryEndpoint}/${encodeURIComponent(cardId)}/original`,
    (value): value is GalleryOriginalResponse => originalValidator(value),
    { signal },
  );
}
