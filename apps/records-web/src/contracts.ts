import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";

import recordsApiSchema from "../../../contracts/records/v1/records-api.schema.json";

const ajv = new Ajv2020({ allErrors: true, strict: true });
addFormats(ajv);
const validator = ajv.compile(recordsApiSchema);

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

const participantSlots = ["participant-a", "participant-b", "participant-c"] as const;
type ParticipantSlot = (typeof participantSlots)[number];

function isParticipantSlot(value: unknown): value is ParticipantSlot {
  return participantSlots.some((slot) => slot === value);
}

function validatedVoteCounts(record: Record<string, unknown>): Map<ParticipantSlot, number> | null {
  if (!isObject(record.result) || !Array.isArray(record.result.voteCounts)) {
    return null;
  }
  const counts = new Map<ParticipantSlot, number>();
  for (const item of record.result.voteCounts) {
    if (
      !isObject(item) ||
      !isParticipantSlot(item.participant) ||
      typeof item.count !== "number" ||
      !Number.isInteger(item.count) ||
      counts.has(item.participant)
    ) {
      return null;
    }
    counts.set(item.participant, item.count);
  }
  if (
    counts.size !== participantSlots.length ||
    [...counts.values()].reduce((total, count) => total + count, 0) !== participantSlots.length
  ) {
    return null;
  }
  const highestCount = Math.max(...counts.values());
  const leaders = participantSlots.filter((slot) => counts.get(slot) === highestCount);
  if (
    !isParticipantSlot(record.result.winner) ||
    !leaders.includes(record.result.winner) ||
    record.result.tieBreakApplied !== leaders.length > 1
  ) {
    return null;
  }
  return counts;
}

function hasConsistentRecordInvariants(value: unknown): boolean {
  if (!isObject(value)) {
    return false;
  }
  if ("items" in value) {
    return (
      Array.isArray(value.items) &&
      value.items.every((item) => isObject(item) && validatedVoteCounts(item) !== null)
    );
  }
  if (!("finalDecision" in value)) {
    return true;
  }
  const summaryCounts = validatedVoteCounts(value);
  if (
    summaryCounts === null ||
    !isObject(value.result) ||
    !isObject(value.finalDecision) ||
    value.result.winner !== value.finalDecision.winner ||
    !Array.isArray(value.votes)
  ) {
    return false;
  }
  const ballotCounts = new Map<ParticipantSlot, number>(participantSlots.map((slot) => [slot, 0]));
  for (const vote of value.votes) {
    if (
      !isObject(vote) ||
      !isParticipantSlot(vote.voter) ||
      !isParticipantSlot(vote.candidate) ||
      vote.voter === vote.candidate
    ) {
      return false;
    }
    ballotCounts.set(vote.candidate, (ballotCounts.get(vote.candidate) ?? 0) + 1);
  }
  return participantSlots.every((slot) => ballotCounts.get(slot) === summaryCounts.get(slot));
}

export function isRecordsApiResponse(value: unknown): boolean {
  return validator(value) && hasConsistentRecordInvariants(value);
}
