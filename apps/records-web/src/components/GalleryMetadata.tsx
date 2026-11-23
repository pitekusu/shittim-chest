import { useState } from "react";

import type { GalleryCard } from "../api/gallery";
import type { AvatarRef } from "../api/types";
import { galleryCollectorName, galleryRarityAriaLabel, galleryRarityLabel } from "../lib/gallery";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/gallery.module.css";
import { Avatar } from "./Avatar";

export function GalleryCollectorIcon({ avatar }: { readonly avatar: AvatarRef | null }) {
  return (
    <span className={styles.metadataAvatar} aria-hidden="true">
      <Avatar avatar={avatar ?? { kind: "placeholder", alt: "", fallbackVariant: "cyan" }} />
    </span>
  );
}

export function GalleryCollector({ card }: { readonly card: GalleryCard }) {
  const name = galleryCollectorName(card);
  return (
    <span className={styles.metadata} title={name}>
      <GalleryCollectorIcon avatar={card.collector?.avatar ?? null} />
      <span className={styles.metadataText}>{name}</span>
    </span>
  );
}

export function GallerySchoolIcon({ logo }: { readonly logo: string | undefined }) {
  const [failedLogo, setFailedLogo] = useState<string>();
  return (
    <span className={styles.schoolIcon} aria-hidden="true">
      {logo && failedLogo !== logo ? (
        <img src={logo} alt="" decoding="async" onError={() => setFailedLogo(logo)} />
      ) : (
        "校"
      )}
    </span>
  );
}

export function GallerySchool({
  schoolName,
  logo,
}: {
  readonly schoolName: string;
  readonly logo: string | undefined;
}) {
  return (
    <span className={styles.metadata} title={schoolName}>
      <GallerySchoolIcon logo={logo} />
      <span className={styles.metadataText}>{schoolName}</span>
    </span>
  );
}

export function GalleryRarity({ rarity }: { readonly rarity: GalleryCard["rarity"] }) {
  return (
    <>
      <span className={commonStyles.visuallyHidden}>{galleryRarityAriaLabel(rarity)}</span>
      <span aria-hidden="true">{galleryRarityLabel(rarity)}</span>
    </>
  );
}
