import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import styles from "../styles/memorial.module.css";

const STANDARD_DURATION_MS = 3_000;
const REDUCED_DURATION_MS = 160;

export function MemorialEntryTransition({
  requesterName,
  onComplete,
}: {
  readonly requesterName: string;
  readonly onComplete: () => void;
}): React.JSX.Element {
  const [reducedMotion] = useState(
    () =>
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  const onCompleteRef = useRef(onComplete);
  const transitionRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    onCompleteRef.current = onComplete;
  }, [onComplete]);

  useEffect(() => {
    const element = transitionRef.current;
    if (element === null) return;
    if (typeof element.showModal === "function") element.showModal();
    else element.setAttribute("open", "");
    element.focus();
    return () => {
      if (element.open && typeof element.close === "function") element.close();
      else element.removeAttribute("open");
    };
  }, []);

  useEffect(() => {
    const timeout = window.setTimeout(
      () => onCompleteRef.current(),
      reducedMotion ? REDUCED_DURATION_MS : STANDARD_DURATION_MS,
    );
    return () => window.clearTimeout(timeout);
  }, [reducedMotion]);

  return createPortal(
    <dialog
      ref={transitionRef}
      className={styles.entryTransition}
      data-reduced-motion={reducedMotion}
      aria-label="メモリアルロビーへログインしています"
      aria-live="polite"
      aria-busy="true"
      tabIndex={-1}
      onCancel={(event) => {
        event.preventDefault();
      }}
    >
      <div className={styles.entryGeometry} aria-hidden="true">
        <span />
        <span />
        <span />
      </div>
      <div className={styles.entrySeal} aria-hidden="true">
        <span>♥</span>
      </div>
      <p className={styles.entryEyebrow} lang="en">
        MEMORIAL LOBBY
      </p>
      <h1>{requesterName}</h1>
      <p className={styles.entryMessage}>大切な思い出をひらいています</p>
      <div className={styles.entryProgress} aria-hidden="true">
        <span />
      </div>
      <p className={styles.entryStatus} lang="en">
        AFFECTION LINK / AUTHORIZED
      </p>
    </dialog>,
    document.body,
  );
}
