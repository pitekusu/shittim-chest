import { infiniteQueryOptions } from "@tanstack/react-query";

import type { ArchiveFilters } from "../hooks/useRecordsArchive";

export function archiveListQuery(
  { winner, sort, search, requester }: ArchiveFilters,
  csrfToken: string,
) {
  const searchActive = search !== "" || requester !== "";
  return infiniteQueryOptions({
    queryKey: ["records", searchActive ? "search" : "list", winner, sort, search, requester],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam, signal }) => {
      // Keep the response validators outside the initial bundle.
      const { getRecords, searchRecords } = await import("../api/recordList");
      signal.throwIfAborted();
      const filters = { cursor: pageParam, sort, winner: winner || undefined };
      return searchActive
        ? searchRecords(
            { ...filters, search, requester: requester || undefined },
            csrfToken,
            signal,
          )
        : getRecords(filters, signal);
    },
    getNextPageParam: (lastPage) => lastPage.nextCursor ?? undefined,
  });
}
