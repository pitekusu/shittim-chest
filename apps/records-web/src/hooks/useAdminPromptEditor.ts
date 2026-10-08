import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  applyAdminPrompts,
  getAdminPrompts,
  getAdminRevision,
  getAdminRevisions,
  rollbackAdminPrompts,
} from "../api/admin";
import { getAdminStatus } from "../api/adminStatus";
import { RecordsApiError } from "../api/http";
import type {
  AdminApplyRequest,
  AdminPromptKey,
  AdminPrompts,
  AdminPromptsResponse,
  AdminRollbackRequest,
} from "../api/types";
import { deriveAdminPromptApplicationState } from "../lib/adminPromptState";
import { useAuthenticationRecovery } from "./useAuthenticationRecovery";

export const PROMPT_LIMIT_BYTES = 3_500;
export const SYSTEM_CONFIRMATION = "APPLY SYSTEM PROMPT";
export const PROMPT_KEYS: readonly AdminPromptKey[] = [
  "system",
  "moderator",
  "participantA",
  "participantB",
  "participantC",
];

interface RetainedIdempotencyKey {
  readonly payload: string;
  readonly value: string;
}

type Synchronization =
  | { readonly kind: "conflict" }
  | { readonly kind: "saved"; readonly draftVersion: number };

function idempotencyKeyFor(
  reference: { current: RetainedIdempotencyKey | null },
  request: AdminApplyRequest | AdminRollbackRequest,
): string {
  const payload = JSON.stringify(request);
  if (reference.current?.payload !== payload) {
    reference.current = { payload, value: crypto.randomUUID() };
  }
  return reference.current.value;
}

export function normalizePrompt(value: string): string {
  return value.replaceAll("\r\n", "\n").replaceAll("\r", "\n").normalize("NFC");
}

function normalizePrompts(prompts: AdminPrompts): AdminPrompts {
  return {
    system: normalizePrompt(prompts.system),
    moderator: normalizePrompt(prompts.moderator),
    participantA: normalizePrompt(prompts.participantA),
    participantB: normalizePrompt(prompts.participantB),
    participantC: normalizePrompt(prompts.participantC),
  };
}

export function promptBytes(value: string): number {
  return new TextEncoder().encode(normalizePrompt(value)).byteLength;
}

export function promptIsValid(value: string): boolean {
  const normalized = normalizePrompt(value);
  return normalized.trim().length > 0 && promptBytes(normalized) <= PROMPT_LIMIT_BYTES;
}

function promptsEqual(left: AdminPrompts, right: AdminPrompts): boolean {
  const normalizedLeft = normalizePrompts(left);
  const normalizedRight = normalizePrompts(right);
  return PROMPT_KEYS.every((key) => normalizedLeft[key] === normalizedRight[key]);
}

export function isRevisionConflict(error: unknown): boolean {
  return (
    error instanceof RecordsApiError &&
    error.status === 409 &&
    error.code === "PROMPT_REVISION_CONFLICT"
  );
}

export function useAdminPromptEditor(canWrite: boolean, csrfToken: string) {
  const queryClient = useQueryClient();
  const prompts = useQuery({
    queryKey: ["admin", "prompts"],
    queryFn: ({ signal }) => getAdminPrompts(signal),
  });
  const status = useQuery({
    queryKey: ["admin", "status"],
    queryFn: ({ signal }) => getAdminStatus(signal),
  });
  const revisions = useInfiniteQuery({
    queryKey: ["admin", "prompt-revisions"],
    queryFn: ({ pageParam, signal }) => getAdminRevisions(pageParam, signal),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  });
  const [baseSnapshot, setBaseSnapshot] = useState<AdminPromptsResponse | null>(null);
  const [drafts, setDrafts] = useState<AdminPrompts | null>(null);
  const [confirmation, setConfirmation] = useState("");
  const [selectedRevision, setSelectedRevision] = useState<string | null>(null);
  const [rollbackTarget, setRollbackTarget] = useState<string | null>(null);
  const [rollbackConfirmation, setRollbackConfirmation] = useState("");
  const [successMessage, setSuccessMessage] = useState<string | null>(null);
  const [latestConflict, setLatestConflict] = useState<AdminPromptsResponse | null>(null);
  const [synchronization, setSynchronization] = useState<Synchronization | null>(null);
  const [syncError, setSyncError] = useState<unknown>(null);
  const [syncPending, setSyncPending] = useState(false);
  const draftVersion = useRef(0);
  const applyIdempotencyKey = useRef<RetainedIdempotencyKey | null>(null);
  const rollbackIdempotencyKey = useRef<RetainedIdempotencyKey | null>(null);

  const revision = useQuery({
    queryKey: ["admin", "prompt-revision", selectedRevision],
    queryFn: ({ signal }) => {
      if (selectedRevision === null) throw new Error("admin_prompt_revision_unavailable");
      return getAdminRevision(selectedRevision, signal);
    },
    enabled: selectedRevision !== null,
  });
  useAuthenticationRecovery(prompts.error);
  useAuthenticationRecovery(status.error);
  useAuthenticationRecovery(revisions.error);
  useAuthenticationRecovery(revision.error);

  useEffect(() => {
    if (baseSnapshot !== null || prompts.data === undefined) return;
    // The initial validated response seeds a private draft once; refetches cannot overwrite it.
    // oxlint-disable-next-line react/set-state-in-effect
    setBaseSnapshot(prompts.data);
    // oxlint-disable-next-line react/set-state-in-effect
    setDrafts(prompts.data.prompts);
  }, [baseSnapshot, prompts.data]);

  async function synchronize(request: Synchronization): Promise<void> {
    setSynchronization(request);
    setSyncError(null);
    setSyncPending(true);
    try {
      const latest = await prompts.refetch();
      // Refetch failures retain cached data. Only a successful GET is authoritative.
      if (!latest.isSuccess) {
        setSyncError(latest.error);
        return;
      }
      if (request.kind === "conflict") {
        setLatestConflict(latest.data);
      } else {
        setBaseSnapshot(latest.data);
        if (draftVersion.current === request.draftVersion) setDrafts(latest.data.prompts);
        setLatestConflict(null);
      }
      setSynchronization(null);
    } catch (error) {
      setSyncError(error);
    } finally {
      setSyncPending(false);
    }
  }

  async function loadSavedRevision(message: string, savedDraftVersion: number): Promise<void> {
    setSelectedRevision(null);
    setRollbackTarget(null);
    setRollbackConfirmation("");
    rollbackIdempotencyKey.current = null;
    queryClient.removeQueries({ queryKey: ["admin", "prompt-revision"] });
    setConfirmation("");
    setSuccessMessage(message);
    await Promise.all([
      synchronize({ kind: "saved", draftVersion: savedDraftVersion }),
      queryClient.invalidateQueries({ queryKey: ["admin", "prompt-revisions"] }),
    ]);
  }

  const applyMutation = useMutation({
    mutationFn: async () => {
      if (baseSnapshot === null || drafts === null) {
        throw new Error("admin_prompt_state_unavailable");
      }
      const savedDraftVersion = draftVersion.current;
      const normalizedDrafts = normalizePrompts(drafts);
      const request: AdminApplyRequest = {
        schemaVersion: 1,
        baseRevision: baseSnapshot.activeRevision,
        prompts: normalizedDrafts,
        systemConfirmation:
          normalizedDrafts.system === normalizePrompt(baseSnapshot.prompts.system)
            ? null
            : confirmation,
      };
      const response = await applyAdminPrompts(
        request,
        csrfToken,
        idempotencyKeyFor(applyIdempotencyKey, request),
      );
      return { response, savedDraftVersion };
    },
    onSuccess: async ({ response, savedDraftVersion }) => {
      applyIdempotencyKey.current = null;
      await loadSavedRevision(`revision ${response.revision} を保存しました。`, savedDraftVersion);
    },
    onError: async (error) => {
      if (isRevisionConflict(error)) await synchronize({ kind: "conflict" });
    },
  });
  useAuthenticationRecovery(applyMutation.error);

  const rollbackSystemChanged = Boolean(
    rollbackTarget !== null &&
    revision.data?.revision === rollbackTarget &&
    prompts.data !== undefined &&
    normalizePrompt(prompts.data.prompts.system) !== normalizePrompt(revision.data.prompts.system),
  );
  const rollbackSameContent = Boolean(
    revision.data !== undefined &&
    prompts.data !== undefined &&
    promptsEqual(revision.data.prompts, prompts.data.prompts),
  );
  const rollbackMutation = useMutation({
    mutationFn: async () => {
      if (baseSnapshot?.activeRevision == null || rollbackTarget === null) {
        throw new Error("admin_prompt_revision_unavailable");
      }
      const savedDraftVersion = draftVersion.current;
      const request: AdminRollbackRequest = {
        schemaVersion: 1,
        baseRevision: baseSnapshot.activeRevision,
        sourceRevision: rollbackTarget,
        systemConfirmation: rollbackSystemChanged ? rollbackConfirmation : null,
      };
      const response = await rollbackAdminPrompts(
        request,
        csrfToken,
        idempotencyKeyFor(rollbackIdempotencyKey, request),
      );
      return { response, savedDraftVersion };
    },
    onSuccess: async ({ response, savedDraftVersion }) => {
      await loadSavedRevision(
        `revision ${response.revision} を復元版として保存しました。`,
        savedDraftVersion,
      );
    },
    onError: async (error) => {
      if (isRevisionConflict(error)) await synchronize({ kind: "conflict" });
    },
  });
  useAuthenticationRecovery(rollbackMutation.error);

  const normalizedDrafts = drafts === null ? null : normalizePrompts(drafts);
  const systemChanged = Boolean(
    baseSnapshot !== null &&
    normalizedDrafts !== null &&
    normalizePrompt(baseSnapshot.prompts.system) !== normalizedDrafts.system,
  );
  const dirty = Boolean(
    baseSnapshot !== null && drafts !== null && !promptsEqual(baseSnapshot.prompts, drafts),
  );
  const invalidPrompts =
    drafts === null ? PROMPT_KEYS : PROMPT_KEYS.filter((key) => !promptIsValid(drafts[key]));
  const writePending = applyMutation.isPending || rollbackMutation.isPending;
  const awaitingBaseline = synchronization !== null || latestConflict !== null;
  const canApply =
    canWrite &&
    baseSnapshot !== null &&
    drafts !== null &&
    invalidPrompts.length === 0 &&
    (dirty || baseSnapshot.mode === "legacy") &&
    (!systemChanged || confirmation === SYSTEM_CONFIRMATION) &&
    !writePending &&
    !awaitingBaseline;
  const canRollback =
    canWrite &&
    rollbackTarget !== null &&
    revision.data?.revision === rollbackTarget &&
    baseSnapshot?.activeRevision != null &&
    !rollbackSameContent &&
    (!rollbackSystemChanged || rollbackConfirmation === SYSTEM_CONFIRMATION) &&
    !writePending &&
    !awaitingBaseline;

  function cancelRollback(): void {
    setRollbackTarget(null);
    setRollbackConfirmation("");
    rollbackIdempotencyKey.current = null;
    if (!rollbackMutation.isPending) rollbackMutation.reset();
  }

  return {
    prompts,
    status,
    revisions,
    revision,
    baseSnapshot,
    drafts,
    confirmation,
    setConfirmation,
    selectedRevision,
    selectRevision: setSelectedRevision,
    rollbackTarget,
    rollbackConfirmation,
    setRollbackConfirmation,
    successMessage,
    latestConflict,
    synchronization,
    syncError,
    syncPending,
    applyMutation,
    rollbackMutation,
    systemChanged,
    invalidPrompts,
    canApply,
    canRollback,
    rollbackSameContent,
    rollbackSystemChanged,
    isLegacyRegistration: baseSnapshot?.mode === "legacy" && !dirty,
    allRevisions: revisions.data?.pages.flatMap((page) => page.items) ?? [],
    selectedIsCurrent:
      revision.data !== undefined && revision.data.revision === prompts.data?.activeRevision,
    applicationState: deriveAdminPromptApplicationState(prompts.data, status.data),
    retrySynchronization: () => {
      if (synchronization !== null && !syncPending) void synchronize(synchronization);
    },
    updateDraft: (key: AdminPromptKey, value: string) => {
      if (!canWrite || drafts === null) return;
      draftVersion.current += 1;
      applyIdempotencyKey.current = null;
      if (!applyMutation.isPending) applyMutation.reset();
      setSuccessMessage(null);
      setDrafts({ ...drafts, [key]: value });
    },
    adoptLatestRevision: () => {
      if (latestConflict === null) return;
      setBaseSnapshot(latestConflict);
      setLatestConflict(null);
      applyIdempotencyKey.current = null;
      rollbackIdempotencyKey.current = null;
      applyMutation.reset();
      rollbackMutation.reset();
    },
    startRollback: (target: string) => {
      cancelRollback();
      setSelectedRevision(target);
      setRollbackTarget(target);
    },
    cancelRollback,
    closeRevision: () => {
      setSelectedRevision(null);
      cancelRollback();
    },
  };
}
