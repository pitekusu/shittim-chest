import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";

import type { ParticipantSlot, SortOrder } from "../api/types";

interface ArchiveFilters {
  readonly winner: ParticipantSlot | "";
  readonly sort: SortOrder;
  readonly search: string;
  readonly requester: string;
}

interface ArchiveReturnTarget {
  readonly recordId: string;
  readonly scrollY: number;
}

interface ArchiveViewState {
  readonly filters: ArchiveFilters;
  readonly returnTarget: ArchiveReturnTarget | null;
}

interface ArchiveActions {
  readonly updateFilters: (changes: Partial<ArchiveFilters>) => void;
  readonly rememberRecord: (recordId: string) => void;
  readonly clearReturnTarget: () => void;
  readonly reset: () => void;
}

type RecordsArchiveState = ArchiveViewState & ArchiveActions;

const INITIAL_FILTERS: ArchiveFilters = { winner: "", sort: "newest", search: "", requester: "" };
const ArchiveViewContext = createContext<ArchiveViewState | null>(null);
const ArchiveActionsContext = createContext<ArchiveActions | null>(null);

// Search can contain question text. Keep it outside the remounted route scene,
// exclusively in React memory; history state and browser storage can persist it.
export function RecordsArchiveProvider({ children }: PropsWithChildren): React.JSX.Element {
  const [filters, setFilters] = useState(INITIAL_FILTERS);
  const [returnTarget, setReturnTarget] = useState<ArchiveReturnTarget | null>(null);
  const updateFilters = useCallback((changes: Partial<ArchiveFilters>) => {
    setFilters((current) => ({ ...current, ...changes }));
  }, []);
  const rememberRecord = useCallback((recordId: string) => {
    setReturnTarget({ recordId, scrollY: window.scrollY });
  }, []);
  const clearReturnTarget = useCallback(() => setReturnTarget(null), []);
  const reset = useCallback(() => {
    setFilters(INITIAL_FILTERS);
    setReturnTarget(null);
  }, []);
  const view = useMemo(() => ({ filters, returnTarget }), [filters, returnTarget]);
  const actions = useMemo(
    () => ({ updateFilters, rememberRecord, clearReturnTarget, reset }),
    [updateFilters, rememberRecord, clearReturnTarget, reset],
  );
  return (
    <ArchiveActionsContext.Provider value={actions}>
      <ArchiveViewContext.Provider value={view}>{children}</ArchiveViewContext.Provider>
    </ArchiveActionsContext.Provider>
  );
}

export function useOptionalRecordsArchiveActions(): ArchiveActions | null {
  return useContext(ArchiveActionsContext);
}

export function useRecordsArchiveActions(): ArchiveActions {
  const actions = useOptionalRecordsArchiveActions();
  if (actions === null) throw new Error("RecordsArchiveProvider is required");
  return actions;
}

export function useOptionalRecordsArchive(): RecordsArchiveState | null {
  const view = useContext(ArchiveViewContext);
  const actions = useOptionalRecordsArchiveActions();
  return useMemo(
    () => (view === null || actions === null ? null : { ...view, ...actions }),
    [view, actions],
  );
}

export function useRecordsArchive(): RecordsArchiveState {
  const state = useOptionalRecordsArchive();
  if (state === null) throw new Error("RecordsArchiveProvider is required");
  return state;
}
