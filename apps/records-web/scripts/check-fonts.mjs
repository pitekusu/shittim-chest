import { createHash } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";

const fonts = new URL("../src/assets/fonts/", import.meta.url);
const generated = new URL("web/", fonts);
const manifest = JSON.parse(await readFile(new URL("manifest.json", generated), "utf8"));
if (manifest.schemaVersion !== 1 || manifest.faces?.length !== 3) {
  throw new Error("web_fonts_manifest_invalid");
}
const sha256 = (bytes) => createHash("sha256").update(bytes).digest("hex");
const expectedFiles = new Set(["manifest.json"]);
for (const face of manifest.faces) {
  const original = await readFile(new URL(face.source, fonts));
  if (sha256(original) !== face.sourceSha256)
    throw new Error(`web_fonts_source_changed: ${face.source}`);
  for (const shard of face.shards) {
    expectedFiles.add(shard.file);
    const contents = await readFile(new URL(shard.file, generated));
    if (contents.byteLength !== shard.bytes || sha256(contents) !== shard.sha256) {
      throw new Error(`web_fonts_derivative_changed: ${shard.file}`);
    }
  }
}
const actualFiles = await readdir(generated);
if (
  actualFiles.length !== expectedFiles.size ||
  actualFiles.some((file) => !expectedFiles.has(file))
) {
  throw new Error("web_fonts_file_set_changed");
}
if (
  sha256(await readFile(new URL("../src/styles/fonts.generated.css", import.meta.url))) !==
  manifest.cssSha256
) {
  throw new Error("web_fonts_styles_changed");
}
console.log(`Verified ${expectedFiles.size - 1} Web font shards and preserved originals`);
