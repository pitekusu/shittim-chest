import { useState } from "react";

import type { AvatarRef } from "../api/types";
import styles from "../styles/common.module.css";

export function Avatar({ avatar }: { readonly avatar: AvatarRef }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  if (avatar.kind === "image" && avatar.url && avatar.url !== failedUrl) {
    return (
      <img
        className={styles.avatar}
        src={avatar.url}
        alt={avatar.alt}
        referrerPolicy="no-referrer"
        onError={() => setFailedUrl(avatar.url ?? null)}
      />
    );
  }
  return (
    <span className={`${styles.avatar} ${styles[`avatar-${avatar.fallbackVariant}`]}`}>
      <span aria-hidden="true" />
      <span className={styles.visuallyHidden}>{avatar.alt}</span>
    </span>
  );
}
