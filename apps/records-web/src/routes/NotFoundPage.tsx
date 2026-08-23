import { Link } from "react-router-dom";

import commonStyles from "../styles/common.module.css";
import routeStyles from "../styles/routeMotion.module.css";

export function NotFoundPage(): React.JSX.Element {
  return (
    <section
      className={`${commonStyles.messagePanel} ${routeStyles.routeMotionItem}`}
      data-route-motion-ready=""
      data-route-motion-terminal=""
    >
      <span className={commonStyles.errorRing} aria-hidden="true">
        ?
      </span>
      <h1 tabIndex={-1}>ページが見つかりません</h1>
      <p>指定されたページは存在しないか、閲覧できません。</p>
      <Link className={commonStyles.primaryButton} to="/">
        記録一覧へ戻る
      </Link>
    </section>
  );
}
