import type { AdminPromptsResponse, AdminStatusResponse } from "../api/types";

export type AdminPromptApplicationState =
  | "legacy"
  | "saved"
  | "next-task"
  | "natural-stop"
  | "applied";

export const ADMIN_PROMPT_APPLICATION_LABELS: Readonly<
  Record<AdminPromptApplicationState, string>
> = {
  legacy: "既存設定",
  saved: "保存済み",
  "next-task": "次回task待ち",
  "natural-stop": "自然停止待ち",
  applied: "適用済み",
};

function uniqueMetric(
  section: AdminStatusResponse["sections"][number],
  name: string,
): string | number | boolean | null | undefined {
  const matches = section.metrics.filter((metric) => metric.name === name);
  return matches.length === 1 ? matches[0]?.value : undefined;
}

function nonnegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export function deriveAdminPromptApplicationState(
  prompts: AdminPromptsResponse | undefined,
  status: AdminStatusResponse | undefined,
): AdminPromptApplicationState {
  if (prompts?.mode === "legacy") return "legacy";
  if (prompts?.mode !== "managed" || prompts.activeRevision === null) return "saved";
  if (status === undefined || status.stale) return "saved";

  const ecsSections = status.sections.filter((section) => section.service === "ecs");
  const ecs = ecsSections.length === 1 ? ecsSections[0] : undefined;
  if (ecs === undefined || ecs.state === "unknown") return "saved";

  const runtimeRevision = uniqueMetric(ecs, "runtime_prompt_revision");
  const desired = nonnegativeInteger(uniqueMetric(ecs, "desired_count"));
  const running = nonnegativeInteger(uniqueMetric(ecs, "running_count"));

  if (runtimeRevision === prompts.activeRevision) return "applied";
  if ((desired !== null && desired > 0) || (running !== null && running > 0)) {
    return "natural-stop";
  }
  if (desired === 0 && running === 0) return "next-task";
  return "saved";
}
