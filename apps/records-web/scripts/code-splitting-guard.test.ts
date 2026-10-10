// @vitest-environment node

import { describe, expect, test } from "vite-plus/test";

import {
  assertCodeSplittingModuleOwnership,
  staticClosure,
  type GuardChunk,
} from "./code-splitting-guard";

const root = "/workspace/apps/records-web";

function canonicalChunks(): GuardChunk[] {
  const routeModules = {
    RecordsHome: ["generated/record-list-response-validator.mjs"],
    RecordDetail: ["generated/record-detail-response-validator.mjs", "components/VoteGraph.tsx"],
    RankingsPage: [
      "generated/rankings-response-validator.mjs",
      "generated/costs-response-validator.mjs",
    ],
    AdminPage: [],
    AdminPromptsPage: [
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
    MomotalkPage: [
      "generated/momotalk-weeks-response-validator.mjs",
      "generated/momotalk-rooms-response-validator.mjs",
      "generated/momotalk-room-response-validator.mjs",
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
      imports: [
        "assets/index.js",
        ...(["AdminPage", "AdminPromptsPage"].includes(route) ? ["assets/admin-status.js"] : []),
      ],
      isEntry: false,
      moduleIds: [`routes/${route}.tsx`, ...modules].map((module) => `${root}/src/${module}`),
    })),
    {
      facadeModuleId: null,
      fileName: "assets/admin-status.js",
      imports: ["assets/index.js"],
      isEntry: false,
      moduleIds: [`${root}/src/generated/admin-status-response-validator.mjs`],
    },
  ];
}

describe("code splitting module ownership", () => {
  test("accepts route-private validators and VoteGraph", () => {
    expect(() => assertCodeSplittingModuleOwnership(canonicalChunks())).not.toThrow();
  });

  test.each(["RecordsHome", "AdminPromptsPage", "MemorialPage", "MomotalkPage"])(
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

  test("rejects the admin status validator shared with a non-admin route", () => {
    const chunks = canonicalChunks();
    chunks
      .find((chunk) => chunk.fileName === "assets/RecordsHome.js")!
      .imports.push("assets/admin-status.js");
    expect(() => assertCodeSplittingModuleOwnership(chunks)).toThrow(
      "code_splitting_module_graph_shared_route_module",
    );
  });

  test("rejects prompt validators shared with the service status route", () => {
    const chunks = canonicalChunks();
    chunks
      .find((chunk) => chunk.fileName === "assets/AdminPage.js")!
      .imports.push("assets/AdminPromptsPage.js");
    expect(() => assertCodeSplittingModuleOwnership(chunks)).toThrow(
      "code_splitting_module_graph_shared_route_module",
    );
  });

  test("counts each static dependency once, including a circular shared import", () => {
    const chunks = canonicalChunks();
    const entry = chunks[0]!;
    entry.imports.push("assets/shared.js");
    chunks.push({
      facadeModuleId: null,
      fileName: "assets/shared.js",
      imports: [entry.fileName],
      isEntry: false,
      moduleIds: [],
    });
    expect(
      [...staticClosure(entry, new Map(chunks.map((chunk) => [chunk.fileName, chunk])))].sort(),
    ).toEqual(["assets/index.js", "assets/shared.js"]);
  });
});
