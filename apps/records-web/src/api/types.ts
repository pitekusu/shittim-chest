export type ParticipantSlot = "participant-a" | "participant-b" | "participant-c";
export type SortOrder = "newest" | "oldest";
export type AdminPromptKey =
  | "system"
  | "moderator"
  | "participantA"
  | "participantB"
  | "participantC";

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

export interface AffectionRankingEntry {
  readonly rank: number;
  readonly displayName: string;
  readonly avatar: AvatarRef;
  readonly score: number;
  readonly resetCount?: number;
}

export interface ParticipantAffectionRanking {
  readonly participant: ParticipantSlot;
  readonly displayName: string;
  readonly entries: readonly AffectionRankingEntry[];
}

export interface AffectionRankingsResponse {
  readonly schemaVersion: 1;
  readonly generatedAt: string;
  readonly defaultScore: 500;
  readonly maxScore: 1000;
  readonly rankings: readonly ParticipantAffectionRanking[];
  readonly nextCursor: string | null;
}

export interface ParticipantAffectionChange {
  readonly participant: ParticipantSlot;
  readonly before: number;
  readonly questionScore: number | null;
  readonly appliedDelta: number;
  readonly after: number;
}

export interface DebateAffection {
  readonly status: "applied" | "unavailable";
  readonly rubricVersion: string;
  readonly participants: readonly ParticipantAffectionChange[];
}

export type CostPeriod = "today" | "week" | "month" | "all";

export interface CostsResponse {
  readonly schemaVersion: 1;
  readonly period: CostPeriod;
  readonly timeZone: "Asia/Tokyo";
  readonly startDate: string;
  readonly endDate: string;
  readonly currency: "JPY";
  readonly total: string;
  readonly breakdown: {
    readonly fargate: string;
    readonly lambda: string;
    readonly openai: string;
    readonly otherAws: string;
  };
  readonly conversion: {
    readonly source: "frankfurter-v2";
    readonly method: "daily-reference-rate";
    readonly baseCurrency: "USD";
    readonly updatedAt: string | null;
  };
  readonly updatedAt: string | null;
  readonly status: "partial" | "final" | "unavailable";
}

export interface RecordDetailResponse {
  readonly schemaVersion: 2;
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
  readonly affection: DebateAffection | null;
}

export type SessionResponse =
  | {
      readonly schemaVersion: 1;
      readonly authenticated: false;
      readonly isAdmin?: false;
      readonly user: null;
      readonly csrfToken: null;
    }
  | {
      readonly schemaVersion: 1;
      readonly authenticated: true;
      readonly isAdmin?: boolean;
      readonly user: {
        readonly displayName: string;
        readonly avatar: AvatarRef;
      };
      readonly csrfToken: string;
    };

export type AdminPrompts = Readonly<Record<AdminPromptKey, string>>;

export interface AdminPromptsResponse {
  readonly schemaVersion: 1;
  readonly mode: "legacy" | "managed";
  readonly activeRevision: string | null;
  readonly createdAt: string | null;
  readonly action: "publish" | "rollback" | null;
  readonly prompts: AdminPrompts;
}

export interface AdminApplyRequest {
  readonly schemaVersion: 1;
  readonly baseRevision: string | null;
  readonly prompts: AdminPrompts;
  readonly systemConfirmation: string | null;
}

export interface AdminApplyResponse {
  readonly schemaVersion: 1;
  readonly revision: string;
  readonly state: "saved";
}

export interface AdminRevisionSummary {
  readonly revision: string;
  readonly createdAt: string;
  readonly action: "publish" | "rollback";
  readonly baseRevision: string | null;
  readonly sourceRevision: string | null;
  readonly checksum: string;
}

export interface AdminRevisionsResponse {
  readonly schemaVersion: 1;
  readonly items: readonly AdminRevisionSummary[];
  readonly nextCursor: string | null;
}

export interface AdminRevisionResponse extends AdminRevisionSummary {
  readonly schemaVersion: 1;
  readonly prompts: AdminPrompts;
}

export interface AdminRollbackRequest {
  readonly schemaVersion: 1;
  readonly baseRevision: string;
  readonly sourceRevision: string;
  readonly systemConfirmation: string | null;
}

export type AdminService =
  | "ecs"
  | "ecr"
  | "inspector"
  | "s3"
  | "dynamodb"
  | "lambda"
  | "cloudfront"
  | "sqs"
  | "apigateway"
  | "eventbridge"
  | "cloudformation"
  | "sns"
  | "ssm"
  | "cost_governance"
  | "signer"
  | "external";
export type AdminHealthState = "healthy" | "warning" | "critical" | "unknown";
export type AdminAlarmCode =
  | "bot-not-ready"
  | "heartbeat-stale"
  | "ingress-runtime-mismatch"
  | "idle-still-running"
  | "reconciler-failure"
  | "status-publish-failure"
  | "outbox-backlog"
  | "dynamo-db-throttle";

export interface AdminActiveAlarm {
  readonly code: AdminAlarmCode;
  readonly severity: "critical" | "warning";
  readonly service: AdminService;
}

export interface AdminEcsDetails {
  readonly kind: "ecs";
  readonly nextTaskImageTags: readonly string[];
}

export interface AdminEcrDetails {
  readonly kind: "ecr";
  readonly images: readonly {
    readonly tags: readonly string[];
    readonly mediaType: "OCI_IMAGE" | "OCI_INDEX" | "DOCKER_V2" | "DOCKER_LIST" | "OTHER";
    readonly sizeBytes: number | null;
    readonly pushedAt: string | null;
    readonly lastPulledAt: string | null;
  }[];
}

export interface AdminInspectorDetails {
  readonly kind: "inspector";
  readonly images: readonly {
    readonly tags: readonly string[];
    readonly scanStatus: "ACTIVE" | "INACTIVE" | "UNKNOWN";
    readonly lastScannedAt: string | null;
    readonly counts: {
      readonly total: number;
      readonly critical: number;
      readonly high: number;
      readonly medium: number;
      readonly low: number;
      readonly untriaged: number;
    };
    readonly findings: readonly {
      readonly vulnerabilityId: string;
      readonly severity: "critical" | "high";
      readonly summaryJa: string | null;
      readonly affectedPackages: readonly {
        readonly name: string;
        readonly installedVersion: string;
        readonly fixedVersion: string | null;
        readonly packageManager: string | null;
      }[];
      readonly fixAvailable: "YES" | "NO" | "PARTIAL" | null;
    }[];
  }[];
}

export type AdminStatusDetails = AdminEcsDetails | AdminEcrDetails | AdminInspectorDetails;

export interface AdminStatusResponse {
  readonly schemaVersion: 1;
  readonly generatedAt: string;
  readonly expiresAt: string;
  readonly stale: boolean;
  readonly overall: {
    readonly state: AdminHealthState;
    readonly criticalAlarms: number;
    readonly warningAlarms: number;
    readonly partial: boolean;
    readonly activeAlarms?: readonly AdminActiveAlarm[];
  };
  readonly sections: readonly {
    readonly service: AdminService;
    readonly state: AdminHealthState;
    readonly summary: string;
    readonly metrics: readonly {
      readonly name: string;
      readonly value: string | number | boolean | null;
    }[];
    readonly details?: AdminStatusDetails | null;
  }[];
}

export interface RecordListFilters {
  readonly cursor?: string;
  readonly sort?: SortOrder;
  readonly winner?: ParticipantSlot;
}
