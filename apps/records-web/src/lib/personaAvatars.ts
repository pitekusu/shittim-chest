import type { AvatarRef, ParticipantSlot } from "../api/types";

export const personaAvatars: Readonly<Record<ParticipantSlot, Omit<AvatarRef, "alt">>> = {
  "participant-a": {
    kind: "image",
    url: new URL("../../scripts/og-image-assets/participant-a.webp", import.meta.url).href,
    fallbackVariant: "cyan",
  },
  "participant-b": {
    kind: "image",
    url: new URL("../../scripts/og-image-assets/participant-b.webp", import.meta.url).href,
    fallbackVariant: "pink",
  },
  "participant-c": {
    kind: "image",
    url: new URL("../../scripts/og-image-assets/participant-c.webp", import.meta.url).href,
    fallbackVariant: "lavender",
  },
};
