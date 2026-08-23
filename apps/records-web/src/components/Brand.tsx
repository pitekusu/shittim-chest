import styles from "../styles/common.module.css";

export function BrandMark({ compact = false }: { readonly compact?: boolean }) {
  return (
    <span className={compact ? styles.brandMarkCompact : styles.brandMark} aria-hidden="true">
      <span />
    </span>
  );
}

function ProductNameLines() {
  return (
    <>
      <span className={styles.productNameLine}>THE SHITTIM</span>
      <span className={styles.productNameLine}>CHEST ARCHIVE</span>
    </>
  );
}

export function ProductName({ headingId }: { readonly headingId?: string }) {
  const accessibleName = "The Shittim Chest Archive";
  if (headingId) {
    return (
      <h1
        id={headingId}
        className={`${styles.productName} ${styles.japaneseHeading}`}
        aria-label={accessibleName}
        lang="en"
      >
        <ProductNameLines />
      </h1>
    );
  }
  return (
    <span className={styles.productName} aria-label={accessibleName} lang="en">
      <ProductNameLines />
    </span>
  );
}
