import type { MemorialUploadContentType } from "./types";

const MAX_MEMORIAL_UPLOAD_BYTES = 10 * 1024 * 1024;
const MEMORIAL_UPLOAD_CONTENT_TYPES = new Set<MemorialUploadContentType>([
  "image/jpeg",
  "image/png",
  "image/webp",
]);

export function isMemorialUploadContentType(value: string): value is MemorialUploadContentType {
  return MEMORIAL_UPLOAD_CONTENT_TYPES.has(value as MemorialUploadContentType);
}

export function validateMemorialImage(file: Pick<File, "type" | "size">): string | null {
  if (!isMemorialUploadContentType(file.type)) {
    return "JPEG、PNG、WebPのいずれかを選んでください。";
  }
  if (file.size < 1 || file.size > MAX_MEMORIAL_UPLOAD_BYTES) {
    return "画像は10 MiB以下にしてください。";
  }
  return null;
}
