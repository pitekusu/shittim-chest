import { isRecordsApiResponse } from "./contracts";

export type ParticipantSlot = "participant-a" | "participant-b" | "participant-c";

export interface AvatarRef {
  readonly kind: "image" | "placeholder";
  readonly url?: string | null;
  readonly alt: string;
  readonly fallbackVariant: "cyan" | "pink" | "lavender";
}

export interface ParticipantSummary {
  readonly slot: ParticipantSlot;
  readonly displayName: string;
  readonly avatar: AvatarRef;
}

export interface RequesterSummary {
  readonly displayName: string;
  readonly avatar: AvatarRef;
}

export interface VoteCount {
  readonly participant: ParticipantSlot;
  readonly count: number;
}

export interface ResultSummary {
  readonly winner: ParticipantSlot;
  readonly voteCounts: readonly VoteCount[];
  readonly tieBreakApplied: boolean;
}

export interface RecordListItem {
  readonly schemaVersion: 1;
  readonly recordId: string;
  readonly completedAt: string;
  readonly questionPreview: string;
  readonly requester: RequesterSummary;
  readonly participants: readonly ParticipantSummary[];
  readonly result: ResultSummary;
}

export interface RecordListResponse {
  readonly schemaVersion: 1;
  readonly items: readonly RecordListItem[];
  readonly nextCursor: string | null;
}

export interface RecordDetailResponse {
  readonly schemaVersion: 1;
  readonly recordId: string;
  readonly completedAt: string;
  readonly question: string;
  readonly requester: RequesterSummary;
  readonly participants: readonly ParticipantSummary[];
  readonly initialOpinions: readonly {
    readonly participant: ParticipantSlot;
    readonly summary: string;
    readonly proposal: string;
  }[];
  readonly finalProposals: readonly {
    readonly participant: ParticipantSlot;
    readonly title: string;
    readonly proposal: string;
  }[];
  readonly votes: readonly {
    readonly voter: ParticipantSlot;
    readonly candidate: ParticipantSlot;
    readonly reason: string;
  }[];
  readonly result: ResultSummary;
  readonly finalDecision: {
    readonly winner: ParticipantSlot;
    readonly victoryMessage: string | null;
    readonly decision: string;
    readonly actions: readonly string[];
    readonly caveats: readonly string[];
  };
}

export type SessionResponse =
  | {
      readonly schemaVersion: 1;
      readonly authenticated: false;
      readonly user: null;
      readonly csrfToken: null;
    }
  | {
      readonly schemaVersion: 1;
      readonly authenticated: true;
      readonly user: {
        readonly displayName: string;
        readonly avatar: AvatarRef;
      };
      readonly csrfToken: string;
    };

interface ErrorResponse {
  readonly error: {
    readonly code: string;
    readonly message: string;
    readonly requestId: string;
  };
}

export class RecordsApiError extends Error {
  public constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly requestId: string,
  ) {
    super(message);
  }
}

function isErrorResponse(value: unknown): value is ErrorResponse {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof value.error === "object" &&
    value.error !== null &&
    "code" in value.error &&
    "message" in value.error &&
    "requestId" in value.error &&
    typeof value.error.code === "string" &&
    typeof value.error.message === "string" &&
    typeof value.error.requestId === "string"
  );
}

async function requestJson(path: string, init?: RequestInit): Promise<unknown> {
  const headers = new Headers(init?.headers);
  headers.set("Accept", "application/json");
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers,
  });
  const value: unknown = await response.json();
  if (!isRecordsApiResponse(value)) {
    throw new RecordsApiError(
      response.status,
      "INVALID_API_RESPONSE",
      "サーバーから不正な応答を受信しました。",
      "local-validation",
    );
  }
  if (!response.ok) {
    if (!isErrorResponse(value)) {
      throw new RecordsApiError(
        response.status,
        "INVALID_ERROR_RESPONSE",
        "エラー応答を確認できませんでした。",
        "local-validation",
      );
    }
    throw new RecordsApiError(
      response.status,
      value.error.code,
      value.error.message,
      value.error.requestId,
    );
  }
  return value;
}

export async function getSession(): Promise<SessionResponse> {
  return (await requestJson("/api/v1/session")) as SessionResponse;
}

export interface RecordListFilters {
  readonly cursor?: string;
  readonly from?: string;
  readonly to?: string;
  readonly winner?: ParticipantSlot;
}

export async function getRecords(filters: RecordListFilters): Promise<RecordListResponse> {
  const query = new URLSearchParams({ limit: "12" });
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== "") {
      query.set(key, value);
    }
  }
  return (await requestJson(`/api/v1/records?${query.toString()}`)) as RecordListResponse;
}

export async function getRecord(recordId: string): Promise<RecordDetailResponse> {
  return (await requestJson(
    `/api/v1/records/${encodeURIComponent(recordId)}`,
  )) as RecordDetailResponse;
}

export async function logout(csrfToken: string): Promise<void> {
  const response = await fetch("/api/v1/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
  });
  if (response.status !== 204) {
    const value: unknown = await response.json();
    if (isRecordsApiResponse(value) && isErrorResponse(value)) {
      throw new RecordsApiError(
        response.status,
        value.error.code,
        value.error.message,
        value.error.requestId,
      );
    }
    throw new RecordsApiError(
      response.status,
      "LOGOUT_FAILED",
      "ログアウトできませんでした。",
      "local-validation",
    );
  }
}
