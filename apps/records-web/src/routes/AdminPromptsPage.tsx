import AdminPromptManager from "../components/AdminPromptManager";
import adminStyles from "../styles/admin.module.css";
import commonStyles from "../styles/common.module.css";
import routeStyles from "../styles/routeMotion.module.css";

export default function AdminPromptsPage({
  isAdmin,
  csrfToken,
}: {
  readonly isAdmin: boolean;
  readonly csrfToken: string;
}): React.JSX.Element {
  return (
    <div className={adminStyles.adminPage} data-route-motion-ready="">
      <header className={`${commonStyles.pageHeader} ${routeStyles.routeMotionItem}`}>
        <p className={commonStyles.eyebrow} lang="en">
          PROMPT MANAGEMENT
        </p>
        <h1
          className={`${commonStyles.japaneseText} ${commonStyles.japaneseHeading}`}
          tabIndex={-1}
        >
          プロンプト管理
        </h1>
      </header>
      <AdminPromptManager canWrite={isAdmin} csrfToken={csrfToken} />
    </div>
  );
}
