import { describe, expect, test } from "vite-plus/test";

import { assertCodeSplittingModuleOwnership, type GuardChunk } from "./code-splitting-guard";

const root = "/workspace/apps/records-web";

function canonicalChunks(): GuardChunk[] {
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
    {
      facadeModuleId: `${root}/src/routes/RecordsHome.tsx`,
      fileName: "assets/RecordsHome.js",
      imports: ["assets/index.js"],
      isEntry: false,
      moduleIds: [
        `${root}/src/routes/RecordsHome.tsx`,
        `${root}/src/generated/record-list-response-validator.mjs`,
      ],
    },
    {
      facadeModuleId: `${root}/src/routes/RecordDetail.tsx`,
      fileName: "assets/RecordDetail.js",
      imports: ["assets/index.js"],
      isEntry: false,
      moduleIds: [
        `${root}/src/routes/RecordDetail.tsx`,
        `${root}/src/generated/record-detail-response-validator.mjs`,
        `${root}/src/components/VoteGraph.tsx`,
      ],
    },
    {
      facadeModuleId: `${root}/src/routes/RankingsPage.tsx`,
      fileName: "assets/RankingsPage.js",
      imports: ["assets/index.js"],
      isEntry: false,
      moduleIds: [
        `${root}/src/routes/RankingsPage.tsx`,
        `${root}/src/generated/rankings-response-validator.mjs`,
        `${root}/src/generated/costs-response-validator.mjs`,
      ],
    },
    {
      facadeModuleId: `${root}/src/routes/AdminPage.tsx`,
      fileName: "assets/AdminPage.js",
      imports: ["assets/index.js"],
      isEntry: false,
      moduleIds: [
        `${root}/src/routes/AdminPage.tsx`,
        `${root}/src/generated/admin-status-response-validator.mjs`,
      ],
    },
  ];
}

describe("code splitting module ownership", () => {
  test("accepts route-private validators and VoteGraph", () => {
    expect(() => assertCodeSplittingModuleOwnership(canonicalChunks())).not.toThrow();
  });

  test("rejects a route validator hoisted into the initial entry", () => {
    const chunks = canonicalChunks();
    chunks[0]?.moduleIds.push(`${root}/src/generated/record-list-response-validator.mjs`);
    chunks[1]?.moduleIds.pop();

    expect(() => assertCodeSplittingModuleOwnership(chunks)).toThrow(
      "code_splitting_module_graph_wrong_owner",
    );
  });

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

  test("rejects an Admin validator hoisted into the initial entry", () => {
    const chunks = canonicalChunks();
    chunks[0]?.moduleIds.push(`${root}/src/generated/admin-status-response-validator.mjs`);
    chunks[4]?.moduleIds.splice(1, 1);

    expect(() => assertCodeSplittingModuleOwnership(chunks)).toThrow(
      "code_splitting_module_graph_wrong_owner",
    );
  });
});
