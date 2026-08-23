export type ParticipantSlot = "participant-a" | "participant-b" | "participant-c";
export type SortOrder = "newest" | "oldest";

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

export interface RankingEntry {
  readonly rank: number;
  readonly displayName: string;
  readonly avatar: AvatarRef;
  readonly count: number;
}

export interface RankingsResponse {
  readonly schemaVersion: 1;
  readonly wins: readonly RankingEntry[];
  readonly requests: readonly RankingEntry[];
  readonly generatedAt: string;
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

export interface RecordListFilters {
  readonly cursor?: string;
  readonly sort?: SortOrder;
  readonly winner?: ParticipantSlot;
}
