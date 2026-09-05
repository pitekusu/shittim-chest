import { describe, expect, test } from "vite-plus/test";

import { assertCodeSplittingModuleOwnership, type GuardChunk } from "./code-splitting-guard";

const root = "/workspace/apps/records-web";

function canonicalChunks(): GuardChunk[] {
  const routeModules = {
    RecordsHome: ["generated/record-list-response-validator.mjs"],
    RecordDetail: ["generated/record-detail-response-validator.mjs", "components/VoteGraph.tsx"],
    RankingsPage: [
      "generated/rankings-response-validator.mjs",
      "generated/costs-response-validator.mjs",
    ],
    AdminPage: [
      "generated/admin-status-response-validator.mjs",
      "generated/admin-prompts-response-validator.mjs",
      "generated/admin-apply-response-validator.mjs",
      "generated/admin-revisions-response-validator.mjs",
      "generated/admin-revision-response-validator.mjs",
    ],
    MemorialPage: [
      "generated/memorial-state-response-validator.mjs",
      "generated/memorial-upload-response-validator.mjs",
      "generated/memorial-memory-response-validator.mjs",
    ],
  };
  return [
    {
      facadeModuleId: `${root}/index.html`,
      fileName: "assets/index.js",
      imports: [],
      isEntry: true,
      moduleIds: [
        `${root}/src/App.tsx`,
        `${root}/src/generated/error-response-validator.mjs`,
        `${root}/src/generated/session-response-validator.mjs`,
      ],
    },
    ...Object.entries(routeModules).map(([route, modules]) => ({
      facadeModuleId: `${root}/src/routes/${route}.tsx`,
      fileName: `assets/${route}.js`,
      imports: ["assets/index.js"],
      isEntry: false,
      moduleIds: [`routes/${route}.tsx`, ...modules].map((module) => `${root}/src/${module}`),
    })),
  ];
}

describe("code splitting module ownership", () => {
  test("accepts route-private validators and VoteGraph", () => {
    expect(() => assertCodeSplittingModuleOwnership(canonicalChunks())).not.toThrow();
  });

  test.each(["RecordsHome", "AdminPage", "MemorialPage"])(
    "rejects a %s validator hoisted into the initial entry",
    (route) => {
      const chunks = canonicalChunks();
      const owner = chunks.find((chunk) => chunk.fileName === `assets/${route}.js`)!;
      chunks[0]!.moduleIds.push(owner.moduleIds.splice(1, 1)[0]!);

      expect(() => assertCodeSplittingModuleOwnership(chunks)).toThrow(
        "code_splitting_module_graph_wrong_owner",
      );
    },
  );

  test("rejects VoteGraph shared with an unrelated route", () => {
    const chunks = canonicalChunks();
    chunks.push({
      facadeModuleId: null,
      fileName: "assets/shared-vote-graph.js",
      imports: [],
      isEntry: false,
      moduleIds: [`${root}/src/components/VoteGraph.tsx`],
    });
    chunks[2]?.moduleIds.pop();
    chunks[1]?.imports.push("assets/shared-vote-graph.js");
    chunks[2]?.imports.push("assets/shared-vote-graph.js");

    expect(() => assertCodeSplittingModuleOwnership(chunks)).toThrow(
      "code_splitting_module_graph_shared_route_module",
    );
  });
});
