import { describe, expect, it } from "vite-plus/test";

import type { AdminPromptsResponse, AdminStatusResponse } from "../api/types";
import { deriveAdminPromptApplicationState } from "./adminPromptState";

const ACTIVE_REVISION = `r${"1".repeat(26)}`;
const RUNTIME_REVISION = `r${"2".repeat(26)}`;
const prompts: AdminPromptsResponse = {
  schemaVersion: 1,
  mode: "managed",
  activeRevision: ACTIVE_REVISION,
  createdAt: "2026-08-24T03:00:00Z",
  action: "publish",
  prompts: {
    system: "system",
    moderator: "moderator",
    participantA: "a",
    participantB: "b",
    participantC: "c",
  },
};

function status({
  desired = 0,
  running = 0,
  revision = RUNTIME_REVISION,
  state = "healthy",
  stale = false,
}: {
  readonly desired?: number | null;
  readonly running?: number | null;
  readonly revision?: string | null;
  readonly state?: "healthy" | "warning" | "critical" | "unknown";
  readonly stale?: boolean;
} = {}): AdminStatusResponse {
  return {
    schemaVersion: 1,
    generatedAt: "2026-08-24T03:00:00Z",
    expiresAt: "2026-08-24T03:01:00Z",
    stale,
    overall: { state, criticalAlarms: 0, warningAlarms: 0, partial: state === "unknown" },
    sections: [
      {
        service: "ecs",
        state,
        summary: "status",
        metrics: [
          { name: "desired_count", value: desired },
          { name: "running_count", value: running },
          { name: "runtime_prompt_revision", value: revision },
        ],
      },
    ],
  };
}

describe("deriveAdminPromptApplicationState", () => {
  it("reports the exact revision as applied", () => {
    expect(
      deriveAdminPromptApplicationState(
        prompts,
        status({ revision: ACTIVE_REVISION, desired: 1, running: 1 }),
      ),
    ).toBe("applied");
  });

  it("distinguishes natural-stop, next-task, and conservative saved states", () => {
    expect(deriveAdminPromptApplicationState(prompts, status({ desired: 1, running: 1 }))).toBe(
      "natural-stop",
    );
    expect(deriveAdminPromptApplicationState(prompts, status())).toBe("next-task");
    expect(deriveAdminPromptApplicationState(prompts, undefined)).toBe("saved");
    expect(deriveAdminPromptApplicationState(prompts, status({ stale: true }))).toBe("saved");
    expect(deriveAdminPromptApplicationState(prompts, status({ state: "unknown" }))).toBe("saved");
    expect(deriveAdminPromptApplicationState(prompts, status({ desired: null }))).toBe("saved");
  });

  it("keeps legacy mode distinct", () => {
    expect(
      deriveAdminPromptApplicationState(
        { ...prompts, mode: "legacy", activeRevision: null, createdAt: null, action: null },
        undefined,
      ),
    ).toBe("legacy");
  });
});
