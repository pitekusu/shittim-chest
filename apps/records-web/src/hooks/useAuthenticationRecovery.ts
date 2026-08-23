import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { RecordsApiError } from "../api/http";

export const SESSION_QUERY_KEY = ["records-session"] as const;

export function useAuthenticationRecovery(error: unknown): void {
  const client = useQueryClient();

  useEffect(() => {
    if (
      !(error instanceof RecordsApiError) ||
      error.status !== 401 ||
      error.code !== "AUTHENTICATION_REQUIRED"
    ) {
      return;
    }

    void client.invalidateQueries({ queryKey: SESSION_QUERY_KEY, exact: true }).finally(() => {
      client.removeQueries({
        predicate: (query) =>
          query.queryKey[0] === "records" ||
          query.queryKey[0] === "record" ||
          query.queryKey[0] === "rankings",
      });
    });
  }, [client, error]);
}
