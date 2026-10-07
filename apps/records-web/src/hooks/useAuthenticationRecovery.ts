import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { RecordsApiError } from "../api/http";
import { recoverSession } from "../lib/protectedQueries";
import { useOptionalRecordsArchiveActions } from "./useRecordsArchive";

export const SESSION_QUERY_KEY = ["records-session"] as const;

export function useAuthenticationRecovery(error: unknown): void {
  const client = useQueryClient();
  const resetArchive = useOptionalRecordsArchiveActions()?.reset;

  useEffect(() => {
    if (
      !(error instanceof RecordsApiError) ||
      error.status !== 401 ||
      error.code !== "AUTHENTICATION_REQUIRED"
    ) {
      return;
    }

    resetArchive?.();
    void recoverSession(client, SESSION_QUERY_KEY);
  }, [client, error, resetArchive]);
}
