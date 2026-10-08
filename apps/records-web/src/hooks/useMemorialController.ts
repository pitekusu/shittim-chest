import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getMemorialMemory,
  getMemorialState,
  prepareMemorialUpload,
  queueMemorialGeneration,
  resetMemorial,
  uploadMemorialSource,
} from "../api/memorial";
import { validateMemorialImage } from "../api/memorialImage";
import { RecordsApiError } from "../api/http";
import type { MemorialStateResponse, MemorialUploadResponse } from "../api/types";
import { useAuthenticationRecovery } from "./useAuthenticationRecovery";

const GENERATE_CONFIRMATION = "GENERATE MEMORIAL";
const RESET_CONFIRMATION = "RESET AFFECTION";

export type LocalProgress = "idle" | "hashing" | "uploading" | "queueing";
interface GenerationAttempt {
  readonly file: File;
  readonly cycle: number;
  readonly prepareIdempotencyKey: string;
  readonly generateIdempotencyKey: string;
  readonly ticket: MemorialUploadResponse | null;
  readonly uploaded: boolean;
}
interface MemorialConfirmation {
  readonly kind: "generate" | "reset";
  readonly pointerInitiated: boolean;
}

function idempotencyKey(): string {
  return `memorial-${crypto.randomUUID()}`;
}

export function memorialActionMessage(error: unknown): string {
  if (!(error instanceof RecordsApiError)) {
    return "処理を完了できませんでした。通信状態を確認して、もう一度お試しください。";
  }
  const known: Readonly<Record<string, string>> = {
    MEMORIAL_STATE_CONFLICT: "別の操作で状態が更新されました。最新の状態を読み直しました。",
    MEMORIAL_UPLOAD_REQUIRED:
      "画像のアップロードを確認できませんでした。画像を選び直してください。",
    MEMORIAL_UPLOAD_NOT_ALLOWED: "現在の状態では画像をアップロードできません。",
    MEMORIAL_RECOVERY_REQUIRED: "生成済みデータを確認しています。少し待ってから再開してください。",
    MEMORIAL_GENERATION_ATTEMPTS_EXHAUSTED:
      "自動再試行の上限に達しました。管理者に復旧を依頼してください。",
    MEMORIAL_QUEUE_UNAVAILABLE: "生成の受付が混み合っています。しばらくしてからお試しください。",
    MEMORIAL_RESET_NOT_ALLOWED: "生成が完了するまで親愛度をリセットできません。",
  };
  return known[error.code] ?? error.message;
}

export function useMemorialController(csrfToken: string) {
  const client = useQueryClient();
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [dialog, setDialog] = useState<MemorialConfirmation | null>(null);
  const [localProgress, setLocalProgress] = useState<LocalProgress>("idle");
  const [selectedCycle, setSelectedCycle] = useState<number | null>(
    () => client.getQueryData<MemorialStateResponse>(["memorial"])?.latestReadyCycle ?? null,
  );
  const [generationAttempt, setGenerationAttempt] = useState<GenerationAttempt | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const initialState = client.getQueryData<MemorialStateResponse>(["memorial"]);
  const observedCycleRef = useRef<number | null>(initialState?.cycle ?? null);
  const observedUploadCapableRef = useRef<boolean | null>(
    initialState ? initialState.state === "unlocked" || initialState.state === "failed" : null,
  );
  const latestReadyCycleRef = useRef(initialState?.latestReadyCycle ?? null);
  const generationEpochRef = useRef(0);
  const queuedGenerationRef = useRef<{
    readonly cycle: number;
    readonly idempotencyKey: string;
  } | null>(null);
  const recoveryGenerationRef = useRef<{
    readonly cycle: number;
    readonly state: "unlocked" | "failed" | "queued";
    readonly idempotencyKey: string;
  } | null>(null);
  const recoveryResetRef = useRef<{
    readonly cycle: number;
    readonly idempotencyKey: string;
  } | null>(null);

  const stateIsFresh = () => client.getQueryState(["memorial"])?.status === "success";

  const stateQuery = useQuery({
    queryKey: ["memorial"],
    queryFn: ({ signal }) => getMemorialState(signal),
    refetchInterval: (query) => {
      const state = (query.state.data as MemorialStateResponse | undefined)?.state;
      return state === "queued" || state === "generating" ? 3_000 : 30_000;
    },
  });
  useAuthenticationRecovery(stateQuery.error);
  useEffect(() => {
    // Query Cache notifications synchronize local work with successful server updates.
    // Refetch failures keep the accepted state and upload attempt intact.
    return client.getQueryCache().subscribe((event) => {
      if (
        event.type !== "updated" ||
        event.action.type !== "success" ||
        event.query.queryKey.length !== 1 ||
        event.query.queryKey[0] !== "memorial"
      )
        return;
      const state = event.query.state.data as MemorialStateResponse | undefined;
      const recovery = recoveryGenerationRef.current;
      if (
        recovery !== null &&
        (state?.cycle !== recovery.cycle || state.state !== recovery.state)
      ) {
        recoveryGenerationRef.current = null;
      }
      if (
        recoveryResetRef.current !== null &&
        state !== undefined &&
        state.cycle !== recoveryResetRef.current.cycle
      ) {
        recoveryResetRef.current = null;
      }
      if (state === undefined) return;
      if (state.latestReadyCycle !== latestReadyCycleRef.current) {
        latestReadyCycleRef.current = state.latestReadyCycle;
        if (state.latestReadyCycle !== null) setSelectedCycle(state.latestReadyCycle);
      }
      if (queuedGenerationRef.current?.cycle !== state.cycle) queuedGenerationRef.current = null;
      const observedCycle = observedCycleRef.current;
      if (observedCycle === null) {
        observedCycleRef.current = state.cycle;
        observedUploadCapableRef.current = state.state === "unlocked" || state.state === "failed";
        return;
      }
      const cycleAdvanced = state.cycle > observedCycle;
      const uploadCapable = state.state === "unlocked" || state.state === "failed";
      const generationStateEnded =
        state.cycle === observedCycle &&
        observedUploadCapableRef.current === true &&
        !uploadCapable;
      if (cycleAdvanced || state.cycle === observedCycle) {
        observedUploadCapableRef.current = uploadCapable;
      }
      if (!cycleAdvanced && !generationStateEnded) return;
      if (cycleAdvanced) observedCycleRef.current = state.cycle;
      generationEpochRef.current += 1;
      if (fileInputRef.current !== null) fileInputRef.current.value = "";
      setSelectedFile(null);
      setGenerationAttempt(null);
      setFileError(null);
      setActionError(null);
      setDragging(false);
      setDialog(null);
      setLocalProgress("idle");
    });
  }, [client]);

  const selectedMemorySummary =
    stateQuery.data?.memories.find((memory) => memory.cycle === selectedCycle) ?? null;
  const memoryQuery = useQuery({
    queryKey: [
      "memorial",
      "memory",
      selectedMemorySummary?.cycle ?? null,
      selectedMemorySummary?.participant ?? null,
      selectedMemorySummary?.unlockedAt ?? null,
      selectedMemorySummary?.generatedAt ?? null,
    ],
    queryFn: ({ signal }) => getMemorialMemory(selectedMemorySummary!, signal),
    enabled: selectedMemorySummary !== null,
  });
  useAuthenticationRecovery(memoryQuery.error);

  const refreshAfterConflict = useCallback(
    async (error: unknown) => {
      if (error instanceof RecordsApiError && error.status === 409) {
        await client.invalidateQueries({ queryKey: ["memorial"], exact: true });
      }
    },
    [client],
  );

  const cancelStateRefresh = useCallback(
    () => client.cancelQueries({ queryKey: ["memorial"], exact: true }),
    [client],
  );

  const generation = useMutation({
    mutationFn: async (attempt: GenerationAttempt) => {
      const epoch = generationEpochRef.current;
      const cycleIsCurrent = () => {
        const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
        return (
          generationEpochRef.current === epoch &&
          observedCycleRef.current === attempt.cycle &&
          cached?.cycle === attempt.cycle &&
          (cached.state === "unlocked" || cached.state === "failed")
        );
      };
      if (!stateIsFresh() || !cycleIsCurrent()) return null;
      setActionError(null);
      let current = attempt;
      if (current.ticket === null) {
        setLocalProgress("hashing");
        const ticket = await prepareMemorialUpload(
          current.file,
          current.cycle,
          csrfToken,
          current.prepareIdempotencyKey,
        );
        if (!cycleIsCurrent()) return null;
        current = { ...current, ticket };
        setGenerationAttempt(current);
      }
      if (!current.uploaded) {
        if (current.ticket === null) throw new Error("Memorial upload ticket is unavailable");
        setLocalProgress("uploading");
        await uploadMemorialSource(current.ticket, current.file);
        if (!cycleIsCurrent()) return null;
        current = { ...current, uploaded: true };
        setGenerationAttempt(current);
      }
      if (!cycleIsCurrent()) return null;
      setLocalProgress("queueing");
      queuedGenerationRef.current = {
        cycle: current.cycle,
        idempotencyKey: current.generateIdempotencyKey,
      };
      const next = await queueMemorialGeneration(
        current.cycle,
        GENERATE_CONFIRMATION,
        csrfToken,
        current.generateIdempotencyKey,
      );
      return cycleIsCurrent() ? next : null;
    },
    onSuccess: async (next, attempt) => {
      if (next === null) return;
      await cancelStateRefresh();
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (
        observedCycleRef.current !== attempt.cycle ||
        cached?.cycle !== attempt.cycle ||
        (cached.state !== "unlocked" && cached.state !== "failed")
      ) {
        return;
      }
      if (fileInputRef.current !== null) fileInputRef.current.value = "";
      client.setQueryData(["memorial"], next);
      setSelectedFile(null);
      setGenerationAttempt(null);
      setLocalProgress("idle");
    },
    onError: (error, attempt) => {
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (
        observedCycleRef.current !== attempt.cycle ||
        cached?.cycle !== attempt.cycle ||
        (cached.state !== "unlocked" && cached.state !== "failed")
      ) {
        return;
      }
      setLocalProgress("idle");
      setActionError(memorialActionMessage(error));
      void refreshAfterConflict(error);
    },
  });

  const retryGeneration = useMutation({
    mutationFn: async ({
      cycle,
      state,
    }: {
      cycle: number;
      state: "unlocked" | "failed" | "queued";
    }) => {
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (!stateIsFresh() || cached?.cycle !== cycle || cached.state !== state) return null;
      let recovery = recoveryGenerationRef.current;
      if (recovery === null || recovery.cycle !== cycle || recovery.state !== state) {
        const queued = queuedGenerationRef.current;
        recovery = {
          cycle,
          state,
          idempotencyKey:
            state === "queued" && queued?.cycle === cycle
              ? queued.idempotencyKey
              : idempotencyKey(),
        };
        recoveryGenerationRef.current = recovery;
      }
      queuedGenerationRef.current = { cycle, idempotencyKey: recovery.idempotencyKey };
      return queueMemorialGeneration(
        cycle,
        GENERATE_CONFIRMATION,
        csrfToken,
        recovery.idempotencyKey,
      );
    },
    onSuccess: async (next, request) => {
      if (next === null) return;
      await cancelStateRefresh();
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (cached?.cycle !== request.cycle || cached.state !== request.state) return;
      setActionError(null);
      client.setQueryData(["memorial"], next);
    },
    onError: (error, request) => {
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (cached?.cycle !== request.cycle || cached.state !== request.state) return;
      setActionError(memorialActionMessage(error));
      void refreshAfterConflict(error);
    },
  });

  const reset = useMutation({
    mutationFn: async (request: { cycle: number; state: "ready" }) => {
      const { cycle } = request;
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (!stateIsFresh() || cached?.cycle !== cycle || cached.state !== "ready") return null;
      let recovery = recoveryResetRef.current;
      if (recovery === null || recovery.cycle !== cycle) {
        recovery = { cycle, idempotencyKey: idempotencyKey() };
        recoveryResetRef.current = recovery;
      }
      return resetMemorial(cycle, RESET_CONFIRMATION, csrfToken, recovery.idempotencyKey);
    },
    onSuccess: async (next) => {
      if (next === null) return;
      await cancelStateRefresh();
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      // Logout/session recovery may clear this private cache while the POST finishes.
      if (cached === undefined || cached.cycle > next.cycle) return;
      setActionError(null);
      if (fileInputRef.current !== null) fileInputRef.current.value = "";
      setSelectedFile(null);
      setGenerationAttempt(null);
      if (cached.cycle < next.cycle) {
        client.setQueryData(["memorial"], next);
      }
      void client.invalidateQueries({ queryKey: ["affection-rankings"] });
    },
    onError: (error, request) => {
      const cached = client.getQueryData<MemorialStateResponse>(["memorial"]);
      if (cached?.cycle !== request.cycle || cached.state !== request.state) return;
      setActionError(memorialActionMessage(error));
      void refreshAfterConflict(error);
    },
  });
  useAuthenticationRecovery(generation.error);
  useAuthenticationRecovery(retryGeneration.error);
  useAuthenticationRecovery(reset.error);

  const chooseFile = useCallback((file: File | undefined) => {
    if (file === undefined) return;
    const validation = validateMemorialImage(file);
    setFileError(validation);
    setSelectedFile(validation === null ? file : null);
    setGenerationAttempt(null);
    setActionError(null);
  }, []);

  const state = stateQuery.data;
  const actionPending = generation.isPending || retryGeneration.isPending || reset.isPending;
  const busy = actionPending || state?.state === "queued" || state?.state === "generating";
  const canSelectFile = state?.state === "unlocked" || state?.state === "failed";
  const selectedFileLabel =
    selectedFile === null
      ? "画像をドロップ、またはファイルを選択"
      : `${selectedFile.name} · ${(selectedFile.size / 1024 / 1024).toFixed(1)} MiB`;

  const actionsBlocked = actionPending || stateQuery.isError;

  const openConfirmation = (kind: MemorialConfirmation["kind"], pointerInitiated: boolean) => {
    if (actionsBlocked || !stateIsFresh()) return;
    if (kind === "generate" && (!selectedFile || !canSelectFile || generationAttempt)) return;
    if (kind === "reset" && state?.state !== "ready") return;
    setDialog({ kind, pointerInitiated });
  };
  const cancelConfirmation = () => setDialog(null);
  const confirmGeneration = () => {
    setDialog(null);
    if (!state || !selectedFile || busy || !canSelectFile || !stateIsFresh()) return;
    const attempt: GenerationAttempt = {
      file: selectedFile,
      cycle: state.cycle,
      prepareIdempotencyKey: idempotencyKey(),
      generateIdempotencyKey: idempotencyKey(),
      ticket: null,
      uploaded: false,
    };
    setGenerationAttempt(attempt);
    generation.mutate(attempt);
  };
  const confirmReset = () => {
    setDialog(null);
    if (state?.state === "ready" && !busy && stateIsFresh()) {
      reset.mutate({ cycle: state.cycle, state: state.state });
    }
  };
  const discardAttempt = () => {
    if (fileInputRef.current !== null) fileInputRef.current.value = "";
    setSelectedFile(null);
    setGenerationAttempt(null);
    setActionError(null);
  };
  const retryAttempt = () => {
    if (generationAttempt && !actionsBlocked && stateIsFresh())
      generation.mutate(generationAttempt);
  };
  const resumeGeneration = (currentState: "unlocked" | "failed" | "queued") => {
    if (state && !actionsBlocked && stateIsFresh()) {
      retryGeneration.mutate({ cycle: state.cycle, state: currentState });
    }
  };
  return {
    stateQuery,
    memoryQuery,
    selectedFile,
    selectedFileLabel,
    fileError,
    actionError,
    dragging,
    setDragging,
    dialog,
    localProgress,
    selectedCycle,
    setSelectedCycle,
    generationAttempt,
    fileInputRef,
    busy,
    canSelectFile,
    actionsBlocked,
    chooseFile,
    openConfirmation,
    cancelConfirmation,
    confirmGeneration,
    confirmReset,
    discardAttempt,
    retryAttempt,
    resumeGeneration,
  };
}
