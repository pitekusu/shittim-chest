import styles from "./App.module.css";

export function App() {
  return (
    <main className={styles.shell}>
      <div className={styles.grid} aria-hidden="true" />
      <section className={styles.panel} aria-labelledby="records-title">
        <div className={styles.mark} aria-hidden="true">
          <span />
        </div>
        <p className={styles.eyebrow}>THE SHITTIM CHEST</p>
        <h1 id="records-title">シッテムの箱 議事録</h1>
        <p>完了した議論を、あとから静かに振り返るための記録庫です。</p>
        <p className={styles.status}>Records foundation</p>
      </section>
    </main>
  );
}
