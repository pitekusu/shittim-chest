import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const projectDirectory = fileURLToPath(new URL("..", import.meta.url));
const outputDirectory = new URL("../dist/", import.meta.url);
const indexPath = new URL("index.html", outputDirectory);
const expectedRouteChunks = [
  "RecordsHome",
  "RecordDetail",
  "RankingsPage",
  "AdminPage",
  "MemorialPage",
];
const maximumEntryGzipBytes = 113_560;
const voteGraphJavaScriptMarker = "vote-graph";
const voteGraphStyleMarker = "--vote-line";
const adminJavaScriptMarkers = ["/api/v1/admin/status", "criticalAlarms"];
const adminStyleMarker = "--admin-panel-marker";

const indexHtml = await readFile(indexPath, "utf8");
const entryMatch = indexHtml.match(
  /<script\b(?=[^>]*\btype=["']module["'])[^>]*\bsrc=["']([^"']+\.js)["'][^>]*>/,
);

if (!entryMatch?.[1]) {
  throw new Error("code_splitting_entry_chunk_missing");
}

const entryPath = new URL(entryMatch[1], "https://records.invalid/").pathname.replace(/^\//, "");
const entryFile = new URL(entryPath, outputDirectory);
let entryContents;
try {
  entryContents = await readFile(entryFile);
} catch (error) {
  if (
    error instanceof Error &&
    "code" in error &&
    (error.code === "ENOENT" || error.code === "EISDIR")
  ) {
    throw new Error(`code_splitting_entry_chunk_not_found: ${entryPath}`, { cause: error });
  }
  throw error;
}
const entryGzipBytes = gzipSync(entryContents).byteLength;
if (entryGzipBytes >= maximumEntryGzipBytes) {
  throw new Error(
    `code_splitting_entry_gzip_budget: maximum=${maximumEntryGzipBytes - 1} actual=${entryGzipBytes}`,
  );
}

const assetFiles = await readdir(new URL("assets/", outputDirectory));
const routeChunkFiles = expectedRouteChunks.map((chunkName) => {
  const matches = assetFiles.filter(
    (fileName) => fileName.startsWith(`${chunkName}-`) && fileName.endsWith(".js"),
  );
  if (matches.length !== 1) {
    throw new Error(
      `code_splitting_route_chunk_count: ${chunkName} expected=1 actual=${matches.length}`,
    );
  }
  return matches[0];
});
const routeStyleFiles = expectedRouteChunks.map((chunkName) => {
  const matches = assetFiles.filter(
    (fileName) => fileName.startsWith(`${chunkName}-`) && fileName.endsWith(".css"),
  );
  if (matches.length !== 1) {
    throw new Error(
      `code_splitting_route_style_count: ${chunkName} expected=1 actual=${matches.length}`,
    );
  }
  return matches[0];
});

const emittedChunks = new Set([entryPath, ...routeChunkFiles]);
if (emittedChunks.size !== expectedRouteChunks.length + 1) {
  throw new Error("code_splitting_chunks_not_distinct");
}

const emittedJavaScript = new Map(
  await Promise.all(
    assetFiles
      .filter((fileName) => fileName.endsWith(".js"))
      .map(async (fileName) => [
        fileName,
        await readFile(new URL(`assets/${fileName}`, outputDirectory), "utf8"),
      ]),
  ),
);
const emittedStyles = new Map(
  await Promise.all(
    assetFiles
      .filter((fileName) => fileName.endsWith(".css"))
      .map(async (fileName) => [
        fileName,
        await readFile(new URL(`assets/${fileName}`, outputDirectory), "utf8"),
      ]),
  ),
);
const voteGraphJavaScriptOwners = [...emittedJavaScript.entries()]
  .filter(([, contents]) => contents.includes(voteGraphJavaScriptMarker))
  .map(([fileName]) => fileName);
const voteGraphStyleOwners = [...emittedStyles.entries()]
  .filter(([, contents]) => contents.includes(voteGraphStyleMarker))
  .map(([fileName]) => fileName);
if (
  voteGraphJavaScriptOwners.length !== 1 ||
  voteGraphJavaScriptOwners[0] !== routeChunkFiles[1] ||
  voteGraphStyleOwners.length !== 1 ||
  voteGraphStyleOwners[0] !== routeStyleFiles[1]
) {
  throw new Error("code_splitting_vote_graph_leaked_outside_record_detail");
}

for (const marker of adminJavaScriptMarkers) {
  const owners = [...emittedJavaScript.entries()]
    .filter(([, contents]) => contents.includes(marker))
    .map(([fileName]) => fileName);
  if (owners.length !== 1 || owners[0] !== routeChunkFiles[3]) {
    throw new Error(`code_splitting_admin_javascript_leaked: ${marker}`);
  }
}
const adminStyleOwners = [...emittedStyles.entries()]
  .filter(([, contents]) => contents.includes(adminStyleMarker))
  .map(([fileName]) => fileName);
if (adminStyleOwners.length !== 1 || adminStyleOwners[0] !== routeStyleFiles[3]) {
  throw new Error("code_splitting_admin_styles_leaked_outside_admin");
}

console.log(
  `Verified entry (${entryGzipBytes} gzip bytes) and route chunks in ${projectDirectory}: ${[
    entryPath,
    ...routeChunkFiles,
    ...routeStyleFiles,
  ].join(", ")}`,
);
