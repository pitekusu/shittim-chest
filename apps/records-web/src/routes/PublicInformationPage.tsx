import { useEffect } from "react";
import { Link } from "react-router-dom";

import { BrandMark } from "../components/Brand";
import { ThemeSwitch } from "../components/Layout";
import { DATA_DELETION_MAILTO, LEGAL_CONTACT_EMAIL } from "../legalContact";
import { setPublicPageMetadata } from "../lib/pageMetadata";
import commonStyles from "../styles/common.module.css";
import styles from "../styles/public-information.module.css";
import type { Theme } from "../theme";

function PrivacyPolicy(): React.JSX.Element {
  return (
    <>
      <p>
        シッテムの箱（運営：pitekusu）は、Discordを使う通常利用と、端末内のデモを提供しています。
        このポリシーはAndroidアプリと関連する議事録Webサービスでの情報の取扱いを説明します。
      </p>
      <section aria-labelledby="privacy-data">
        <h2 id="privacy-data">利用する情報と目的</h2>
        <ul>
          <li>
            DiscordのユーザーID・ユーザー名・表示名・アイコン・指定サーバーへの参加状態を、
            本人の認証、利用資格の確認、依頼者の表示に利用します。セッション情報も扱います。
          </li>
          <li>
            通常利用で送信した相談本文・添付画像、生成された議論・結果・親愛度などを、
            議論の作成、記録の保存・閲覧・共有に利用します。
          </li>
          <li>
            通知を利用する場合は、通知先の端末識別情報・通知トークンを登録し、結果を知らせます。
            通知には依頼者の表示名や記録の識別情報が含まれます。
          </li>
          <li>
            問い合わせ・削除申請のメールと返信先を、本人確認、対象の確認、対応結果の連絡に利用します。
            不具合対応と安全な運営のため、本文や認証情報を含めない処理記録も保存します。
          </li>
        </ul>
        <p>広告配信や広告目的の追跡は行わず、広告・アクセス解析SDKは導入していません。</p>
      </section>
      <section aria-labelledby="privacy-sharing">
        <h2 id="privacy-sharing">送信先と閲覧範囲</h2>
        <p>
          通常の議論作成では、本文・画像をOpenAIへ送信してAIの処理を行い、Amazon Web
          Services（AWS）へ記録・画像などを保存します。相談内容・添付画像・結果は、
          指定Discordサーバーや当サービスの認可された利用者へ共有されます。
          ご自身や他の方の秘密、公開・共有したくない情報は入力しないでください。
        </p>
        <p>
          議論の質問・依頼者の表示名・アイコンなどは、公開リンクのプレビュー画像にも含まれます。
          プレビューはログインなしで閲覧され、外部サイトにキャッシュされる場合があります。
        </p>
        <p>
          通知にはGoogle
          FirebaseのMessagingとInstallationsを利用し、問い合わせ窓口にはGmailを利用します。 Google
          Playの更新確認も行います。外部事業者側での取扱いには各事業者の規約・ポリシーが適用されます。
        </p>
      </section>
      <section aria-labelledby="privacy-demo">
        <h2 id="privacy-demo">デモと端末内のデータ</h2>
        <p>
          「デモで試す」の本文・選択画像・下書き・定型結果は、通常利用と分離した端末内の領域で扱います。
          デモの内容を本番のAI処理、Discord投稿、当サービスのサーバー保存へ送信しません。
          「デモを終了」または「デモを初期化」でデモの保存データを削除できます。
        </p>
        <p>
          Androidの通常利用では、ログイン情報、閲覧用の記録・画像、下書きなどを暗号化して保存します。
          ログアウトするとこれらの端末内データを削除します。
          ログアウトやアンインストールだけでは、投稿済みのサーバーデータは削除されません。
        </p>
      </section>
      <section aria-labelledby="privacy-retention">
        <h2 id="privacy-retention">保存期間と削除</h2>
        <p>
          議論の記録・受理済み画像・利用者プロフィール・関連する統計には自動の保存期限を設けず、
          サービスの記録として保持します。削除を希望する場合は、
          <Link to="/data-deletion">データ削除の申請</Link>からご連絡ください。
        </p>
        <p>
          認証セッションは最長90日で失効し、未受理の一時画像には原則1日の保存期限があります。
          データベースの復旧用バックアップには最長35日分の履歴が含まれます。
          画像の過去の版、バックアップ、外部事業者の保存内容も確認対象とし、
          削除の対応範囲や残存期間は本人確認後にご案内します。即時の完全消去は保証しません。
        </p>
        <p>
          問い合わせ・申請メールは対応と確認に必要な期間保管し、不要になった内容は削除します。
          本文を含めない処理ログも、運営上必要な範囲で保存先の保持設定に従って保管します。
        </p>
        <p>
          認証・権限の確認、HTTPS通信、保存先のアクセス制限、端末内の暗号化により、情報の保護に努めます。
          内容を変更した場合は、このページの更新日を改めます。
        </p>
      </section>
      <section aria-labelledby="privacy-contact">
        <h2 id="privacy-contact">問い合わせ窓口</h2>
        <p>
          運営：pitekusu
          <br />
          プライバシーに関するお問い合わせ：
          <a href={`mailto:${LEGAL_CONTACT_EMAIL}`}>{LEGAL_CONTACT_EMAIL}</a>
        </p>
      </section>
    </>
  );
}

function DataDeletion(): React.JSX.Element {
  return (
    <>
      <p>
        シッテムの箱が管理する、ご本人に関係するデータの削除を申請できます。
        アプリの再インストール、Discordサーバーへの参加、当サービスへのログインは不要です。
      </p>
      <section className={styles.request} aria-labelledby="deletion-request">
        <h2 id="deletion-request">メールで削除を申請する</h2>
        <p>次のボタンでメールアプリを開き、内容を確認して送信してください。</p>
        <a className={commonStyles.primaryButton} href={DATA_DELETION_MAILTO}>
          データ削除をメールで申請
        </a>
        <p>
          メールアプリが開かない場合は、<a href={DATA_DELETION_MAILTO}>{LEGAL_CONTACT_EMAIL}</a>
          へ、件名「シッテムの箱 データ削除申請」で送信してください。
          このページを開いたりボタンを押したりするだけでは、申請は送信されません。
        </p>
      </section>
      <section aria-labelledby="deletion-identification">
        <h2 id="deletion-identification">申請に記入すること</h2>
        <ul>
          <li>利用時のDiscordユーザー名など、ご本人の利用を確認できる情報（わかる範囲）。</li>
          <li>当サービスのデータすべてか、特定の投稿・画像などか、削除を希望する対象。</li>
        </ul>
        <p>
          ご連絡いただいたメールに返信し、必要な本人確認や対象の確認を行います。
          ログインできない場合も、まずはわかる範囲でご連絡ください。
          パスワード、認証コード、アクセストークンを送る必要はありません。
        </p>
      </section>
      <section aria-labelledby="deletion-scope">
        <h2 id="deletion-scope">対象と対応方法</h2>
        <p>
          当サービスの利用者情報・セッション・通知登録、ご本人の投稿した議論・添付画像などが確認対象です。
          公開プレビュー画像と当サービスの配信キャッシュも対象に含めて確認します。
          本人確認後に運営が手動で削除へ対応し、対応内容・完了状況をメールでお知らせします。
          複数の利用者に関係する共有記録などは、対象と対応方法を確認してご案内します。
          確認後に対象と対応予定をお知らせし、速やかに対応します。
        </p>
        <p>
          Discord自体のアカウントを削除する手続きではありません。
          Discord上の投稿や外部事業者へ送信された情報についても、
          削除に必要な確認・手続きを行い、事業者側の残存期間や別途必要な手続きはご案内します。
        </p>
      </section>
      <section aria-labelledby="deletion-retained">
        <h2 id="deletion-retained">バックアップなどに残る情報</h2>
        <p>
          復旧用データベースのバックアップは最長35日分保持されます。
          画像の過去の版、外部事業者の保存内容などには、別途確認・削除の手続きが必要な場合があります。
          即時にすべての保存先から消えるとは限らないため、対応範囲と残存期間を個別にお知らせします。
        </p>
      </section>
      <section aria-labelledby="deletion-local">
        <h2 id="deletion-local">端末内のデータだけを消す場合</h2>
        <p>
          Androidのデモはメニューの「デモを終了」または「デモを初期化」で削除できます。
          通常利用の端末内のログイン情報・閲覧用データ・下書きは、ログアウトで削除できます。
          これらの操作だけでは、運営側の投稿済み記録は削除されません。
        </p>
        <p>
          詳しい取扱いは<Link to="/privacy-policy">プライバシーポリシー</Link>をご確認ください。
          窓口の運営者はpitekusuです。
        </p>
      </section>
    </>
  );
}

export default function PublicInformationPage({
  page,
  theme,
  onThemeToggle,
}: {
  readonly page: "privacy" | "deletion";
  readonly theme: Theme;
  readonly onThemeToggle: () => void;
}): React.JSX.Element {
  const privacy = page === "privacy";
  const title = privacy ? "プライバシーポリシー" : "データ削除の申請";
  const path = privacy ? "/privacy-policy" : "/data-deletion";
  useEffect(() => {
    setPublicPageMetadata(
      title,
      path,
      privacy
        ? "シッテムの箱の情報の取扱いと問い合わせ窓口。"
        : "シッテムの箱のデータ削除を、ログイン不要でメール申請できます。",
    );
    window.scrollTo(0, 0);
  }, [title, path, privacy]);

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.brand}>
          <BrandMark compact />
          <span>シッテムの箱</span>
        </div>
        <ThemeSwitch theme={theme} onToggle={onThemeToggle} />
      </header>
      <main className={`${styles.content} ${commonStyles.japaneseText}`}>
        <nav className={styles.navigation} aria-label="利用者向けの案内">
          <Link to="/privacy-policy" aria-current={privacy ? "page" : undefined}>
            プライバシーポリシー
          </Link>
          <Link to="/data-deletion" aria-current={!privacy ? "page" : undefined}>
            データ削除の申請
          </Link>
        </nav>
        <h1>{title}</h1>
        <p className={styles.updated}>シッテムの箱 · 更新日：2026年10月12日</p>
        {privacy ? <PrivacyPolicy /> : <DataDeletion />}
      </main>
      <footer className={styles.footer}>シッテムの箱 · pitekusu</footer>
    </div>
  );
}
