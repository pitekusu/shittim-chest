import type { QueryClient, QueryKey } from "@tanstack/react-query";

const PROTECTED_QUERIES = new Set([
  "records",
  "record",
  "rankings",
  "affection-rankings",
  "costs",
  "memorial",
  "momotalk",
  "admin",
]);
const recoveries = new WeakMap<QueryClient, Promise<void>>();

export function isProtectedQuery(queryKey: QueryKey): boolean {
  return typeof queryKey[0] === "string" && PROTECTED_QUERIES.has(queryKey[0]);
}

export function clearProtectedQueries(client: QueryClient): void {
  const filters = {
    predicate: (query: { queryKey: QueryKey }) => isProtectedQuery(query.queryKey),
  };
  void client.cancelQueries(filters);
  client.removeQueries(filters);
}

// Parallel 401s share one session refresh. Failed refresh state belongs to the
// session query; no private error or payload is logged here.
export function recoverSession(client: QueryClient, sessionQueryKey: QueryKey): Promise<void> {
  const pending = recoveries.get(client);
  if (pending) return pending;
  const recovery = client
    .invalidateQueries({ queryKey: sessionQueryKey, exact: true })
    .catch(() => undefined)
    .finally(() => {
      clearProtectedQueries(client);
      recoveries.delete(client);
    });
  recoveries.set(client, recovery);
  return recovery;
}
