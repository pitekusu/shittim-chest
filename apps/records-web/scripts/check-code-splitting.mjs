import { readFile, readdir } from "node:fs/promises";
import { gzipSync } from "node:zlib";
import { JSDOM } from "jsdom";

import { HTML_ENTRY_FILES, staticClosure } from "./code-splitting-guard.ts";

const outputDirectory = new URL("../dist/", import.meta.url);
const expectedRoutes = [
  "RecordsHome",
  "RecordDetail",
  "RankingsPage",
  "AdminPage",
  "AdminPromptsPage",
  "MemorialPage",
  "MomotalkPage",
];
const maximumEntryGzipBytes = 113_560;
const manifest = JSON.parse(
  await readFile(new URL("records-code-splitting.json", outputDirectory), "utf8"),
);
if (manifest.schemaVersion !== 1 || !Array.isArray(manifest.chunks)) {
  throw new Error("code_splitting_manifest_invalid");
}
const chunks = manifest.chunks;
const byFile = new Map(chunks.map((chunk) => [chunk.fileName, chunk]));
const entries = chunks.filter((chunk) => chunk.isEntry);
if (entries.length !== HTML_ENTRY_FILES.length)
  throw new Error("code_splitting_entry_chunk_missing");
const initialClosures = [];
for (const file of HTML_ENTRY_FILES) {
  const matches = entries.filter((entry) => entry.html === file);
  if (matches.length !== 1) throw new Error(`code_splitting_entry_chunk_count: ${file}`);
  const html = await readFile(new URL(file, outputDirectory), "utf8");
  const window = new JSDOM(html).window;
  const scripts = [...window.document.querySelectorAll('script[type="module"][src]')].map(
    (script) => script.getAttribute("src")?.replace(/^\//, "") ?? "",
  );
  window.close();
  if (scripts.length === 0 || scripts.some((name) => !byFile.has(name))) {
    throw new Error(`code_splitting_entry_chunk_mismatch: ${file}`);
  }
  const actual = new Set(scripts.flatMap((name) => [...staticClosure(byFile.get(name), byFile)]));
  const expected = staticClosure(matches[0], byFile);
  // Vite may inline a pure-import HTML entry into module tags and remove its proxy.
  // Compare every dependency with the actual emitted HTML rather than only its first tag.
  if (!actual.has(matches[0].fileName)) expected.delete(matches[0].fileName);
  if (actual.size !== expected.size || [...expected].some((name) => !actual.has(name))) {
    throw new Error(`code_splitting_entry_chunk_mismatch: ${file}`);
  }
  initialClosures.push(actual);
}

const initial = new Set(initialClosures.flatMap((closure) => [...closure]));
const routeClosures = new Map(
  expectedRoutes.map((route) => {
    const matches = chunks.filter((chunk) => chunk.route === route);
    if (matches.length !== 1) throw new Error(`code_splitting_route_chunk_count: ${route}`);
    return [route, staticClosure(matches[0], byFile)];
  }),
);

const assets = await readdir(new URL("assets/", outputDirectory));
const javascript = new Map(
  await Promise.all(
    assets
      .filter((name) => name.endsWith(".js"))
      .map(async (name) => [
        `assets/${name}`,
        await readFile(new URL(`assets/${name}`, outputDirectory)),
      ]),
  ),
);
const styles = new Map(
  await Promise.all(
    assets
      .filter((name) => name.endsWith(".css"))
      .map(async (name) => [
        `assets/${name}`,
        await readFile(new URL(`assets/${name}`, outputDirectory), "utf8"),
      ]),
  ),
);

const initialGzipBytes = Math.max(
  ...initialClosures.map((closure) =>
    [...closure].reduce((total, fileName) => {
      const contents = javascript.get(fileName);
      if (!contents) throw new Error(`code_splitting_static_import_missing: ${fileName}`);
      return total + gzipSync(contents).byteLength;
    }, 0),
  ),
);
if (initialGzipBytes >= maximumEntryGzipBytes) {
  throw new Error(
    `code_splitting_entry_gzip_budget: maximum=${maximumEntryGzipBytes - 1} actual=${initialGzipBytes}`,
  );
}

function cssClosure(closure) {
  return new Set([...closure].flatMap((fileName) => byFile.get(fileName)?.css ?? []));
}

function assertExclusiveAssets(contents, marker, permittedRoutes, kind) {
  const owners = [...contents]
    .filter(([, source]) => source.toString().includes(marker))
    .map(([fileName]) => fileName);
  if (owners.length === 0) throw new Error(`code_splitting_${kind}_marker_missing: ${marker}`);
  const initialAssets = kind === "css" ? cssClosure(initial) : initial;
  for (const fileName of owners) {
    if (initialAssets.has(fileName))
      throw new Error(`code_splitting_${kind}_leaked_to_entry: ${marker}`);
    if (
      !permittedRoutes.some((route) => {
        const closure = routeClosures.get(route);
        return (kind === "css" ? cssClosure(closure) : closure).has(fileName);
      })
    )
      throw new Error(`code_splitting_${kind}_owner_missing: ${marker}`);
    for (const [route, closure] of routeClosures) {
      const routeAssets = kind === "css" ? cssClosure(closure) : closure;
      if (!permittedRoutes.includes(route) && routeAssets.has(fileName)) {
        throw new Error(`code_splitting_${kind}_leaked_to_route: ${marker} ${route}`);
      }
    }
  }
}

assertExclusiveAssets(javascript, "vote-graph", ["RecordDetail"], "javascript");
assertExclusiveAssets(styles, "--vote-line", ["RecordDetail"], "css");
assertExclusiveAssets(
  javascript,
  "/api/v1/admin/status",
  ["AdminPage", "AdminPromptsPage"],
  "javascript",
);
assertExclusiveAssets(
  javascript,
  "criticalAlarms",
  ["AdminPage", "AdminPromptsPage"],
  "javascript",
);
assertExclusiveAssets(javascript, "/api/v1/admin/prompts", ["AdminPromptsPage"], "javascript");
assertExclusiveAssets(styles, "--admin-panel-marker", ["AdminPage", "AdminPromptsPage"], "css");

for (const route of expectedRoutes) {
  if (
    [...cssClosure(routeClosures.get(route))].every((fileName) => cssClosure(initial).has(fileName))
  ) {
    throw new Error(`code_splitting_route_style_missing: ${route}`);
  }
}
console.log(
  `Verified ${entries.length} initial static JS closures (maximum ${initialGzipBytes} gzip bytes) and ${expectedRoutes.length} lazy routes`,
);
