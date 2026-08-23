import { readFile, readdir, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";

const projectDirectory = fileURLToPath(new URL("..", import.meta.url));
const outputDirectory = new URL("../dist/", import.meta.url);
const indexPath = new URL("index.html", outputDirectory);
const expectedRouteChunks = ["RecordsHome", "RecordDetail", "RankingsPage"];
const maximumEntryGzipBytes = 113_560;
const voteGraphJavaScriptMarker = "vote-graph";
const voteGraphStyleMarker = "--vote-line";

const indexHtml = await readFile(indexPath, "utf8");
const entryMatch = indexHtml.match(
  /<script\b(?=[^>]*\btype=["']module["'])[^>]*\bsrc=["']([^"']+\.js)["'][^>]*>/,
);

if (!entryMatch?.[1]) {
  throw new Error("code_splitting_entry_chunk_missing");
}

const entryPath = new URL(entryMatch[1], "https://records.invalid/").pathname.replace(/^\//, "");
const entryFile = new URL(entryPath, outputDirectory);
if (!(await stat(entryFile)).isFile()) {
  throw new Error(`code_splitting_entry_chunk_not_found: ${entryPath}`);
}
const entryGzipBytes = gzipSync(await readFile(entryFile)).byteLength;
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

console.log(
  `Verified entry (${entryGzipBytes} gzip bytes) and route chunks in ${projectDirectory}: ${[
    entryPath,
    ...routeChunkFiles,
    ...routeStyleFiles,
  ].join(", ")}`,
);
