import type { Plugin } from "vite-plus";

export interface GuardChunk {
  readonly facadeModuleId: string | null;
  readonly fileName: string;
  readonly imports: string[];
  readonly isEntry: boolean;
  readonly moduleIds: string[];
}

const ROUTES = {
  RecordsHome: {
    facade: "/src/routes/RecordsHome.tsx",
    ownedModules: ["/src/generated/record-list-response-validator.mjs"],
  },
  RecordDetail: {
    facade: "/src/routes/RecordDetail.tsx",
    ownedModules: [
      "/src/generated/record-detail-response-validator.mjs",
      "/src/components/VoteGraph.tsx",
    ],
  },
  RankingsPage: {
    facade: "/src/routes/RankingsPage.tsx",
    ownedModules: [
      "/src/generated/rankings-response-validator.mjs",
      "/src/generated/costs-response-validator.mjs",
    ],
  },
  AdminPage: {
    facade: "/src/routes/AdminPage.tsx",
    ownedModules: [
      "/src/generated/admin-status-response-validator.mjs",
      "/src/generated/admin-prompts-response-validator.mjs",
      "/src/generated/admin-apply-response-validator.mjs",
      "/src/generated/admin-revisions-response-validator.mjs",
      "/src/generated/admin-revision-response-validator.mjs",
    ],
  },
} as const;

const INITIAL_MODULES = [
  "/src/generated/session-response-validator.mjs",
  "/src/generated/error-response-validator.mjs",
] as const;

function normalizeModuleId(moduleId: string): string {
  return moduleId.replaceAll("\\", "/").split("?", 1)[0] ?? moduleId;
}

function findUniqueChunk(
  chunks: readonly GuardChunk[],
  predicate: (chunk: GuardChunk) => boolean,
  label: string,
): GuardChunk {
  const matches = chunks.filter(predicate);
  if (matches.length !== 1 || matches[0] === undefined) {
    throw new Error(`code_splitting_module_graph_${label}: expected=1 actual=${matches.length}`);
  }
  return matches[0];
}

function staticClosure(
  root: GuardChunk,
  chunksByFileName: ReadonlyMap<string, GuardChunk>,
): Set<string> {
  const closure = new Set<string>();
  const pending = [root.fileName];
  while (pending.length > 0) {
    const fileName = pending.pop();
    if (fileName === undefined || closure.has(fileName)) continue;
    closure.add(fileName);
    const chunk = chunksByFileName.get(fileName);
    if (chunk !== undefined) pending.push(...chunk.imports);
  }
  return closure;
}

export function assertCodeSplittingModuleOwnership(chunks: readonly GuardChunk[]): void {
  const chunksByFileName = new Map(chunks.map((chunk) => [chunk.fileName, chunk]));
  const entry = findUniqueChunk(chunks, (chunk) => chunk.isEntry, "entry_chunk_count");
  const initialClosure = staticClosure(entry, chunksByFileName);
  const routeClosures = new Map<string, Set<string>>();

  for (const [routeName, route] of Object.entries(ROUTES)) {
    const routeChunk = findUniqueChunk(
      chunks,
      (chunk) => normalizeModuleId(chunk.facadeModuleId ?? "").endsWith(route.facade),
      `${routeName}_chunk_count`,
    );
    routeClosures.set(routeName, staticClosure(routeChunk, chunksByFileName));
  }

  const ownerOf = (moduleSuffix: string): GuardChunk =>
    findUniqueChunk(
      chunks,
      (chunk) => chunk.moduleIds.some((moduleId) => moduleId.endsWith(moduleSuffix)),
      `${moduleSuffix.replaceAll("/", "_")}_owner_count`,
    );

  for (const [routeName, route] of Object.entries(ROUTES)) {
    const routeClosure = routeClosures.get(routeName);
    if (routeClosure === undefined) throw new Error("code_splitting_module_graph_route_missing");
    for (const moduleSuffix of route.ownedModules) {
      const owner = ownerOf(moduleSuffix);
      if (initialClosure.has(owner.fileName) || !routeClosure.has(owner.fileName)) {
        throw new Error(`code_splitting_module_graph_wrong_owner: ${moduleSuffix}`);
      }
      for (const [otherRouteName, otherClosure] of routeClosures) {
        if (otherRouteName !== routeName && otherClosure.has(owner.fileName)) {
          throw new Error(`code_splitting_module_graph_shared_route_module: ${moduleSuffix}`);
        }
      }
    }
  }

  for (const moduleSuffix of INITIAL_MODULES) {
    const owner = ownerOf(moduleSuffix);
    if (!initialClosure.has(owner.fileName)) {
      throw new Error(`code_splitting_module_graph_initial_module_deferred: ${moduleSuffix}`);
    }
  }
}

export function codeSplittingModuleOwnershipGuard(): Plugin {
  return {
    name: "records-code-splitting-module-ownership",
    apply: "build",
    generateBundle(_options, bundle) {
      const chunks: GuardChunk[] = Object.values(bundle)
        .filter((output) => output.type === "chunk")
        .map((chunk) => ({
          facadeModuleId: chunk.facadeModuleId,
          fileName: chunk.fileName,
          imports: chunk.imports,
          isEntry: chunk.isEntry,
          moduleIds: Object.keys(chunk.modules).map(normalizeModuleId),
        }));
      assertCodeSplittingModuleOwnership(chunks);
    },
  };
}
