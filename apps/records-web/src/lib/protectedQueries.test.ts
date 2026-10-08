import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vite-plus/test";

import { SESSION_QUERY_KEY } from "../hooks/useAuthenticationRecovery";
import { clearProtectedQueries, recoverSession } from "./protectedQueries";

function client(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
}

describe("protected query lifecycle", () => {
  it("aborts protected reads and removes costs and private data while preserving the session", async () => {
    const queries = client();
    queries.setQueryData(SESSION_QUERY_KEY, { authenticated: true });
    queries.setQueryData(["costs", "week"], { snapshot: "cost fixture" });
    queries.setQueryData(["admin", "prompts"], { snapshot: "prompt fixture" });
    queries.setQueryData(["memorial", "memory", 1], { snapshot: "memory fixture" });
    queries.setQueryData(["momotalk", "weeks"], { snapshot: "week fixture" });
    queries.setQueryData(["public-metadata"], { title: "Archive" });
    let signal: AbortSignal | undefined;
    const pending = queries
      .fetchQuery({
        queryKey: ["record", "fixture"],
        queryFn: (context) => {
          signal = context.signal;
          return new Promise<never>(() => undefined);
        },
      })
      .catch((error: unknown) => error);

    clearProtectedQueries(queries);

    expect(signal?.aborted).toBe(true);
    await pending;
    expect(
      queries
        .getQueryCache()
        .getAll()
        .map((query) => query.queryKey),
    ).toEqual([SESSION_QUERY_KEY, ["public-metadata"]]);
    expect(queries.getQueryData(SESSION_QUERY_KEY)).toEqual({ authenticated: true });
    expect(queries.getQueryData(["public-metadata"])).toEqual({ title: "Archive" });
    queries.clear();
  });

  it("shares one session refresh across simultaneous 401 recoveries and allows later recovery", async () => {
    const queries = client();
    queries.setQueryData(SESSION_QUERY_KEY, { authenticated: true });
    queries.setQueryData(["costs", "week"], { snapshot: "cost fixture" });
    let resolveSession!: (value: { authenticated: boolean }) => void;
    const refresh = vi.fn<() => Promise<{ authenticated: boolean }>>(
      () =>
        new Promise<{ authenticated: boolean }>((resolve) => {
          resolveSession = resolve;
        }),
    );
    const observer = new QueryObserver(queries, {
      queryKey: SESSION_QUERY_KEY,
      queryFn: refresh,
      staleTime: Infinity,
    });
    const unsubscribe = observer.subscribe(() => undefined);

    const first = recoverSession(queries, SESSION_QUERY_KEY);
    expect(recoverSession(queries, SESSION_QUERY_KEY)).toBe(first);
    expect(recoverSession(queries, SESSION_QUERY_KEY)).toBe(first);
    expect(refresh).toHaveBeenCalledTimes(1);
    resolveSession({ authenticated: false });
    await first;

    expect(queries.getQueryData(["costs", "week"])).toBeUndefined();
    expect(queries.getQueryData(SESSION_QUERY_KEY)).toEqual({ authenticated: false });
    const later = recoverSession(queries, SESSION_QUERY_KEY);
    expect(later).not.toBe(first);
    expect(refresh).toHaveBeenCalledTimes(2);
    resolveSession({ authenticated: false });
    await later;
    unsubscribe();
    queries.clear();
  });

  it("still clears protected data when refreshing the session fails", async () => {
    const queries = client();
    queries.setQueryData(SESSION_QUERY_KEY, { authenticated: true });
    queries.setQueryData(["costs", "month"], { snapshot: "cost fixture" });
    const failure = new Error("offline");
    const observer = new QueryObserver(queries, {
      queryKey: SESSION_QUERY_KEY,
      queryFn: () => Promise.reject(failure),
      staleTime: Infinity,
    });
    const unsubscribe = observer.subscribe(() => undefined);

    await recoverSession(queries, SESSION_QUERY_KEY);

    expect(queries.getQueryData(["costs", "month"])).toBeUndefined();
    expect(queries.getQueryState(SESSION_QUERY_KEY)?.error).toBe(failure);
    expect(queries.getQueryData(SESSION_QUERY_KEY)).toEqual({ authenticated: true });
    unsubscribe();
    queries.clear();
  });
});
