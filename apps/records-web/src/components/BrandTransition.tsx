import { useEffect } from "react";

import authStyles from "../styles/auth.module.css";
import { BrandMark } from "./Brand";

export function BrandTransition({
  accessibleName,
  message,
  onComplete,
}: {
  readonly accessibleName: string;
  readonly message: string;
  readonly onComplete: () => void;
}): React.JSX.Element {
  useEffect(() => {
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const timer = window.setTimeout(onComplete, reduced ? 150 : 2_000);
    return () => window.clearTimeout(timer);
  }, [onComplete]);

  return (
    <output className={authStyles.brandTransition} aria-label={accessibleName}>
      <BrandMark />
      <p lang="en">{message}</p>
    </output>
  );
}
