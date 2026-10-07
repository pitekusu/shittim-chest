import weeksValidator from "../generated/momotalk-weeks-response-validator.mjs";
import roomsValidator from "../generated/momotalk-rooms-response-validator.mjs";
import roomValidator from "../generated/momotalk-room-response-validator.mjs";
import { requestJson } from "./http";
import type { ParticipantSlot, ParticipantSummary, RequesterSummary } from "./types";

export interface MomotalkWeek {
  readonly weekId: string;
  readonly periodStart: string;
  readonly periodEnd: string;
  readonly publishAt: string;
}
export interface MomotalkRoomSummary {
  readonly roomId: string;
  readonly requester: RequesterSummary;
  readonly questionCount: number;
  readonly state: "preparing" | "ready" | "failed";
}
export interface MomotalkWeeksResponse {
  readonly schemaVersion: 1;
  readonly weeks: readonly MomotalkWeek[];
  readonly nextCursor: string | null;
}
export interface MomotalkRoomsResponse {
  readonly schemaVersion: 1;
  readonly week: MomotalkWeek;
  readonly rooms: readonly MomotalkRoomSummary[];
  readonly nextCursor: string | null;
}
export interface MomotalkMessage {
  readonly id: number;
  readonly participant: ParticipantSlot;
  readonly text: string;
}
export interface MomotalkImage {
  readonly participant: ParticipantSlot;
  readonly mood: "happy" | "unhappy";
  readonly state: "pending" | "ready" | "failed";
  readonly url: string | null;
  readonly thumbnailUrl: string | null;
  readonly downloadUrl: string | null;
}
export interface MomotalkRoomResponse {
  readonly schemaVersion: 1;
  readonly week: MomotalkWeek;
  readonly room: MomotalkRoomSummary;
  readonly participants: readonly ParticipantSummary[];
  readonly messages: readonly MomotalkMessage[];
  readonly images: readonly MomotalkImage[];
}

function pageQuery(cursor?: string): string {
  const query = new URLSearchParams({ limit: "50" });
  if (cursor) query.set("cursor", cursor);
  return query.toString();
}
export function getMomotalkWeeks(
  cursor?: string,
  signal?: AbortSignal,
): Promise<MomotalkWeeksResponse> {
  return requestJson(
    `/api/v1/momotalk/weeks?${pageQuery(cursor)}`,
    (value): value is MomotalkWeeksResponse => weeksValidator(value),
    { signal },
  );
}
export function getMomotalkRooms(
  week: string,
  cursor?: string,
  signal?: AbortSignal,
): Promise<MomotalkRoomsResponse> {
  return requestJson(
    `/api/v1/momotalk/weeks/${encodeURIComponent(week)}/rooms?${pageQuery(cursor)}`,
    (value): value is MomotalkRoomsResponse => roomsValidator(value),
    { signal },
  );
}
export function getMomotalkRoom(
  week: string,
  room: string,
  signal?: AbortSignal,
): Promise<MomotalkRoomResponse> {
  return requestJson(
    `/api/v1/momotalk/weeks/${encodeURIComponent(week)}/rooms/${encodeURIComponent(room)}`,
    (value): value is MomotalkRoomResponse => roomValidator(value),
    { signal },
  );
}
