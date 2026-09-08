import type { RecordDetailResponse } from "../api/types";

const ORIGIN = "https://shittim.pitekusu.dev";
const COMMON_IMAGE = `${ORIGIN}/assets/shittim-chest-archive-og-5e706f31f069.png`;
const COMMON_TITLE = "THE SHITTIM CHEST ARCHIVE";
const COMMON_ALT =
  "淡い水色の幾何学模様とTHE SHITTIM CHEST ARCHIVE、シッテムの箱 議事録閲覧システム、アロナ・プラナ・安倍晋三AIのアイコン";
const COMMON_DESCRIPTION =
  "シッテムの箱 議事録閲覧システム。吹雪型JCのつどいサーバの先生であることを認証して、議論記録を閲覧できます。";

function meta(key: string, content: string): void {
  const attr = key.startsWith("og:") ? "property" : "name";
  const matches = document.head.querySelectorAll<HTMLMetaElement>(`meta[${attr}="${key}"]`);
  const element = matches[0] ?? document.createElement("meta");
  matches.forEach((match, index) => {
    if (index > 0) match.remove();
  });
  element.setAttribute(attr, key);
  element.content = content;
  if (!element.isConnected) document.head.append(element);
}

export function setPageMetadata(record?: RecordDetailResponse): void {
  const preview = record?.ogImageUrl ? record : undefined;
  const question = preview?.question.replace(/\s+/gu, " ").slice(0, 160);
  const title = preview ? `${question} | THE SHITTIM CHEST` : COMMON_TITLE;
  const description = preview
    ? `${preview.requester.displayName}の議論の記録 · ${new Intl.DateTimeFormat("ja-JP", { timeZone: "Asia/Tokyo", year: "numeric", month: "long", day: "numeric" }).format(new Date(preview.completedAt))}`
    : COMMON_DESCRIPTION;
  const url = preview ? `${ORIGIN}/records/${preview.recordId}` : `${ORIGIN}/`;
  const image = preview?.ogImageUrl ?? COMMON_IMAGE;
  document.title = preview ? title : `${title} | シッテムの箱 議事録閲覧システム`;
  const canonical =
    document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]') ??
    document.createElement("link");
  canonical.rel = "canonical";
  canonical.href = url;
  if (!canonical.isConnected) document.head.append(canonical);
  for (const [key, value] of Object.entries({
    description,
    "og:title": title,
    "og:description": description,
    "og:type": preview ? "article" : "website",
    "og:url": url,
    "og:image": image,
    "og:image:secure_url": image,
    "og:image:alt": question ?? COMMON_ALT,
    "og:image:width": "1200",
    "og:image:height": "630",
    "og:image:type": "image/png",
    "twitter:card": "summary_large_image",
    "twitter:title": title,
    "twitter:description": description,
    "twitter:image": image,
    "twitter:image:alt": question ?? COMMON_ALT,
    "og:site_name": preview ? "THE SHITTIM CHEST" : "シッテムの箱 議事録",
    "og:locale": "ja_JP",
  }))
    meta(key, value);
  if (preview) meta("robots", "noindex, nofollow");
  else document.head.querySelector('meta[name="robots"]')?.remove();
}
