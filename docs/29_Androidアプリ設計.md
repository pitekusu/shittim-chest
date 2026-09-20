---
aliases: [シッテムの箱 Android, Records Android]
tags: [project, shittim-chest, android]
status: current
created: 2026-09-16
updated: 2026-09-20
---

# Androidアプリ設計

[文書索引へ戻る](00_シッテムの箱_ドキュメント索引.md)

## 目的と現在の範囲

既存のDiscord・Records・Webを維持し、友人向けのAndroidネイティブアプリを段階的に追加する。
現段階はC03までの最小アプリ・デザイン基盤・Android CIと、C05のモバイル認証契約である。製品機能の提供やGoogle Playへの配布を完了した状態ではない。

| 段階 | 内容 | 現在の扱い |
|---|---|---|
| C01 | Gradle Wrapper、Version Catalog、最小Compose画面 | 実装済み |
| デザイン基盤の先行実装 | Expressiveテーマ、独自配色・書体・背景、準備画面 | C01を維持した追加差分。C02の機能実装とは分離 |
| C02 | Circuit・Metroによる準備画面の状態管理・依存接続 | 実装済み |
| C03 | Android CI | 実装済み |
| C04 | CodeQL接続 | Kotlin 2.4.20へのCodeQL対応待ち。GitHub切替は未実施 |
| C05 | モバイル認証の要求・応答・内部状態 | 契約定義のみ実装済み。保存・認証実行・公開ルートは未接続 |
| 後続 | 認証、記録閲覧、暗号化保存、署名済み配布 | 未実装。未使用のAPI・権限は先行追加しない |

### PRの分割単位

実装計画のC01、C02…を、それぞれ独立したPRとして進める。複数のCを1本のPRへまとめない。
原則は番号順だが、2026年9月20日の合意により、独立したC04を保留してC05へ先行する。
C04を完了扱いにはせず、[Issue #376](https://github.com/pitekusu/shittim-chest/issues/376)で安定版の対応と再開を追跡する。
各PRには対象Cの実装・関連試験・文書を含め、同じCの不具合修正もそのPRで扱う。
C01には合意済みのExpressiveデザイン基盤の先行実装を含めるが、C02以降の機能は追加しない。
PRの公開状態はその工程の依頼に従う。C02・C03は確認後に通常PRとして公開した。
必須CI・CodeQL・レビュー状態を確認し、マージを妨げる問題がなければ、許可された範囲でsquash mergeする。
PR内はレビュー可能な目的別コミットに分けてよい。C番号は実装の区切りであり、Gitのコミット数と一致させる必要はない。
C04はCodeQLの対応後に独立したPRで再開し、C02・C03の機能は混ぜない。

### CodeQLのビルドと切替

2026年9月17日の実行確認では、CodeQL 2.27.0がKotlin 2.4.20を未対応として拒否した。
Kotlin／Compose Compilerは2.4.20を維持し、CodeQLの対応版を待つ。
未反映のworkflow案は取り下げ、既存のGitHub自動設定・必須条件は変更しない。
Androidの解析未完了を成功として扱わず、対応版で抽出成功を確認してからGitHub側を切り替える。

再開時は`.github/workflows/codeql.yml`を追加し、Kotlinを`manual`モードで解析する。
TemurinはAndroidの`.java-version`から読み、SDK Platform 37.1とBuild Tools 36.0.0を用意する。
CodeQL初期化後にWrapperから`assembleDebug`を実行し、キャッシュや差分コンパイルによる抽出漏れを防ぐ。
エミュレーター、署名鍵、認証情報は使用しない。通常のAndroid CI（C03）とは役割を分ける。

Python・JavaScript/TypeScript・GitHub Actionsも同じworkflowへ移し、既存のチェック名と
`security-extended`を維持する。PR、mainへのpush、週次定期実行、手動実行を対象にする。
GitHubのdefault setupとadvanced setupは併用せず、切替手順は
[GitHub・CI-CD詳細設計](15_GitHub・CI-CD詳細設計.md)に従う。

### Kotlin Compiler Native Image

Kotlin 2.4.20のNative Image版を単体CLIとして利用する。
GraalVMで事前コンパイルされたKotlin/JVMコンパイラーであり、
アプリをKotlin/Nativeへ移行するものではない。Material 3やJVM targetも変更しない。

2026年9月17日にLinux x86_64の公式配布物をSHA-256照合して導入し、
単体Kotlinのコンパイル・実行とCompose Compilerによる最小Composableの変換を確認した。
`-include-runtime`は配布物内のリソース参照で失敗したため、通常のclass出力と
標準ライブラリを指定する実行方法を使う。上流の非推奨API警告は抑制しない。

Kotlin Gradle Plugin 2.4.20ではNative Image選択の正式な設定は確認できていない。
CLIの導入だけでAGPの
`:app:compileDebugKotlin`が置き換わるわけではなく、APKビルドは従来の方式を維持する。
Gradle連携を独自実装せず、正式な連携経路を確認できた時点で改めて採用する。
CodeQL互換性も別の条件であり、Native Imageを解析回避には使わない。

配布仕様は[Kotlin 2.4.20のNative Image](https://kotlinlang.org/docs/whatsnew2420.html#native-image)、
利用方法はリポジトリの`apps/records-android/README.md`を参照する。

## C02：CircuitとMetroの接続

画面は1つのまま、表示状態とUIの責務を分ける。通信・認証・DB・通知・架空の遷移先は追加しない。

```mermaid
flowchart LR
    Activity[MainActivity] --> Graph[Metro RecordsGraph]
    Graph --> Circuit[CircuitContent]
    Circuit --> Presenter[BootstrapPresenter]
    Presenter -->|State| UI[BootstrapUi]
    UI -->|SelectTheme| Presenter
```

- Circuit 0.39.0とMetro 1.4.4をVersion Catalogで固定する。Kotlin／Compose Compiler 2.4.20とMaterial 3 1.5.0-alpha28は維持する。
- `RecordsGraph`はActivityごとに1回生成し、実際に使うCircuitとPresenterだけを提供する。Application全体のscopeや空のRepositoryは設けない。
- `BootstrapScreen`は固定の画面識別子、`State`は表示選択、`Event.SelectTheme`は操作入力を表す。まだback stackを作らず、画面識別子の永続化もしない。
- `BootstrapPresenter`が`rememberSaveable`で表示選択を保持し、Activity再生成時に復元する。端末設定やアカウント設定には保存しない。
- `BootstrapUi`はStateから描画し、操作をeventSinkへ返す。スクロールなどUI固有の状態はUI側に残す。
- C01のMaterialExpressiveTheme、MotionScheme、排他的ButtonGroup、Expressive List、semantic color、余白と文字拡大時のoverflowを維持する。
- UI Previewは固定Stateを渡して表示し、DIや外部サービスを必要としない。MetroのCircuit codegenやKSPはこの1画面には追加しない。

## C03：Android CI

[CI設計](15_GitHub・CI-CD詳細設計.md)の`android-build`で、ローカルと同じJDK・Wrapper・SDKを使い、
debug APK／テストAPK・LintとAPI 36の画面テストを実行する。画面・認証・配布機能は追加しない。

- C02の既存2件をそのまま実行し、別の大量のテストや端末matrixは設けない。
- Android専用差分では無関係なCore／Recordsの全試験を実行しない。共通CI・分類器の変更は両側を検証する。
- `android-gate`は必要なビルドの失敗・取消・skipや分類失敗を成功扱いしない。
- レポートのみを短期保存し、APK／署名済み配布は後続工程に残す。
- Kotlin 2.4.20とMaterial 3 1.5.0-alpha28は維持する。CodeQL対応待ちのC04は分離する。

## C05：モバイル認証の契約（公開前）

Recordsの`mobile_auth.py`に要求・応答と内部状態を定義する。既存の`PublicModel`、`SessionUser`、
OAuth／セッション有効期間を再利用し、`AuthService`・WebのCookie／CSRF・Discord callbackは変更しない。
APIはC12まで公開せず、C05ではDBアクセス・トークン発行・PKCE照合・状態遷移の実行を行わない。
`mobile-auth.schema.json`は既存の契約生成コマンドで生成するが、live OpenAPIとWeb validatorには混ぜない。

### 受け渡しと期限

Androidはログインごとにランダムなverifierとstateを用意する。verifierは43〜128文字のASCII unreserved文字、
challengeはSHA-256をpaddingなしbase64urlにした43文字、方式は`S256`だけとする。
このPKCEはAndroidとRecords間の一回限りコードの交換を保護するもので、Discord OAuthのPKCE対応を仮定しない。
方式・長さは[RFC 7636](https://www.rfc-editor.org/rfc/rfc7636.html)、外部ブラウザーとApp Linksの方針は
[RFC 8252](https://www.rfc-editor.org/rfc/rfc8252.html)に基づく。

| 境界 | 契約 |
|---|---|
| ログイン取引 | 開始から最長10分。ブラウザー往復で延長しない |
| 一回限りコード | 発行から最長60秒、かつ取引期限まで |
| モバイルセッション | 発行から90日の絶対期限。利用で延長せず、refresh tokenを設けない |
| state・取引ID・コード・Bearer token | 独立した256-bit乱数をpaddingなしbase64urlで表す。stateは端末内の開始値と照合 |
| アプリ復帰先 | Records設定のHTTPS origin＋固定`/auth/mobile/callback`。任意のredirect URIは受け付けない |
| 復帰query | `transaction`・`code`・`state`のみ。アクセストークン・Discord token・ユーザー情報を載せない |
| 認証後の目的画面 | `/`または`/records/{43文字のrecordId}`のみ。query・fragment・外部URL・管理画面は拒否 |

期限ちょうどは失効とし、後続実装で毎回サーバー時刻と比較する。TTL削除を待たない。
App Linksの公開証明書と配布版の接続はC19で確定する。C05はAndroid manifestや公開ページを追加しない。

### 予定するAPIの入出力

以下は未公開の契約であり、ルートはまだ存在しない。全応答を`private, no-store`とする。
JSONはcamelCase、unknown field・余分／重複queryを拒否し、検証例外は固定コードだけへ変換する。

| 予定ルート | 入力／出力 |
|---|---|
| `POST /api/v1/auth/mobile/start` | `codeChallenge`・`codeChallengeMethod`・`state`・省略時`/`の`returnTo` → `schemaVersion`・`transactionId`・`authorizePath`・offset付き`expiresAt` |
| `GET /api/v1/auth/mobile/authorize` | queryの`transaction`のみ。開始端末に結び付く取引とブラウザーの使い捨てCookieを確認してDiscordへ302 |
| 既存Discord callback | 現在の本人・Guild確認を再利用。モバイル取引だけを固定App Linkへ302。失敗時はコード・セッションを発行せず、Web側で安全なエラーを表示 |
| `POST /api/v1/auth/mobile/exchange` | `transactionId`・`code`・`codeVerifier` → `schemaVersion`・`accessToken`・`tokenType: Bearer`・`expiresAt`・`user`・`isAdmin`・`returnTo` |
| `GET /api/v1/auth/mobile/session` | Bearerを確認し、exchange応答からtoken・returnToを除いた本人情報を返す。無効／失効は401 |
| `POST /api/v1/auth/mobile/logout` | Bearerを失効して204。body・queryなし |

`authorizePath`は固定API originに対する相対パスで、任意URLを開かせない。ブラウザー側の一意な取引使用権はC06〜C08で実装する。
Web CookieとBearerの混在拒否、既存所属・管理者認可、Origin／CSRF、body上限はC07〜C12のHTTP接続で検証する。
リクエスト不正は`mobile_request_invalid`、取引・コード・PKCEの不一致／期限切れ／再使用は同一の
`mobile_grant_invalid`として詳細を漏らさない（公開時の400）。セッション不正は既存の401形式を維持する。

### 内部状態と保存境界

```mermaid
stateDiagram-v2
    [*] --> started: 取引作成
    started --> authorizing: ブラウザーが一度だけ取得
    authorizing --> authorized: Discord本人・Guild確認後にコード発行
    authorized --> consumed: S256照合とセッション発行を原子的に実行
```

- 状態ごとの型を判別可能なunionにし、`authorizing`はブラウザーnonce／OAuth stateのハッシュ、
  `authorized`はコードhash・期限・opaque requester key・表示用プロフィール・所属確認日時を必須にする。
- 内部の時刻はepoch秒、APIはoffset付き日時。取引10分・コード60秒と取引期限の包含関係を型の検証で守る。
- コード・verifier・Bearer tokenの平文を内部状態に保存しない。ID・nonce・コードは用途別HMACで保存する設計とする。
  client stateだけはアプリへそのまま返す相関値として保持するが、それだけで認証や交換を許可しない。
- 状態の形の検証と、保存時のCAS／原子更新・有効期限判定は別物。後者と一回限りの保証はC06／C09で実装する。
- `repr`と検証エラー表示へ機密フィールドを出さない。HTTP／adapterでPydanticの`errors()`や入力全体をログに記録しない。
- C05の試験はredirect・S256入力境界・状態必須項目・期限の関係・内部情報の非公開・既存OpenAPI不変に絞る。

## 最小構成

`apps/records-android/`を独立したGradleプロジェクトとし、C01では`:app`の1モジュールだけを置く。
Android Studioで開く場所、必要な環境、実行コマンドは同ディレクトリのREADMEにまとめる。

```mermaid
flowchart LR
    Launcher[Androidランチャー] --> Activity[MainActivity]
    Activity --> Circuit[CircuitContent]
    Circuit --> Compose[Composeの準備画面]
```

- KotlinとJetpack Composeで静的な準備画面を表示する。通信・認証・永続保存は行わない。
- AGPのbuilt-in Kotlinを使用し、旧来のKotlin Androidプラグインは重ねて適用しない。
- Kotlin Gradle PluginとCompose CompilerはVersion Catalogの同じKotlin版へ固定する。
- Gradle Wrapperは配布版とSHA-256を固定し、SDK・JDKはリポジトリへ含めない。
- 実行用JDKはTemurinの`.java-version`指定版、生成するJVMバイトコードは17とする。
- minSdkは26、targetSdkは37。Composeの要件に合わせ、compileSdkは37.1（`platforms;android-37.1`）とする。
- debug版はapplication ID末尾に`.dev`を付け、将来の配布版との混同を防ぐ。
- 通信権限や本番接続先は持たせない。端末バックアップと平文HTTPを許可しない。
- SDKパス、ビルド出力、IDE設定、署名鍵をGitから除外する。

## UI・UXの設計方針

Material 3 Expressiveを操作部品とモーションの基盤に採用し、外観はシッテムの箱の独自Design Systemで構成する。
ブルーアーカイブを想起する透明感と未来的な端末の操作感、3人格の親しみやすさを両立する。
公式ゲームのロゴ・画面素材を新たに流用せず、背景・ライン・図形はComposeで描画する。

### 表現とデザイントークン

| 要素 | 明るい表示 | 暗い表示・共通ルール |
|---|---|---|
| 背景 | 氷色 `#F5FBFF` と淡い水色 `#DFF8FB` | 深い紺 `#071724` と青緑 `#123B49` |
| 本文 | 濃紺 `#17324D`、補助文字 `#526A80` | 白青 `#EAF8FF`、補助文字 `#A8C5D4` |
| 操作の強調 | コントラストを確保したシアン `#1B7189` | 明るいシアン `#80E5F0`。端末壁紙のDynamic Colorへ置換しない |
| パネル | surface系の役割で読みやすい面を作る | 関連情報はExpressive Listへまとめ、各行を独立したカードで囲わない |
| 装飾 | 薄いグリッド、円弧、菱形 | 本文の背後は控えめにし、常時点滅・常時回転・重いblurは使わない |
| 人格色 | アロナはシアン、プラナはピンク、安倍晋三AIはラベンダー | 色だけで識別させず、名前・アイコンを併記する |
| 日本語・数値 | LINE Seed JP Regular／Bold | Materialの通常・強調書体を含め、共通テーマから設定する |
| 英字ブランド | 既存のDelogy | ブランドなど短い装飾見出しだけ。本文や操作説明に使わない |

具体的な値は`ui/ShittimTheme.kt`をAndroid側の正とし、Webの色の役割を踏襲する。
surface・inverse・fixedの各roleも明示し、未指定の既定紫が新しい部品へ混ざらないようにする。
明るい側のprimaryと補助文字は、濃いsurface上でも通常文字のコントラスト比4.5以上になるよう調整する。
背景とシンボルは`ui/ShittimBackdrop.kt`へ分離する。部品を使う前から大量の共通クラス・モジュールは作らない。
LINE Seed JPは既存WebのWOFF2をTTFへ展開して同梱し、字形の加工・サブセット化はしない。
Delogyは既存TTFをそのまま使用する。出典・OFLはAPKの`assets/licenses/`へ含める。

### 操作とモーション

| 場面 | 採用する表現 | 操作上の制約 |
|---|---|---|
| 押下・選択 | Expressiveの形状変化と応答性のある動き | 意図を示す短い反応。入力を待たせない |
| NEW／OLD | 選択位置が連続して移動・変形する切替 | 選択状態を文字・読み上げでも伝える。C35で接続 |
| 一覧から詳細 | 選んだカード・議題を引き継ぐ展開 | 戻る操作とスクロール位置を維持。長文自体は動かさない |
| ログイン完了 | 約2秒の接続・認証演出 | 実際の認証成功後のみ。通常起動・画面復帰に強制待機を入れない |
| 読み込み | 円弧・リング等の動きと状態文言 | 実処理中だけ。取得できない進捗率を表示しない |
| モーション低減 | Androidのアニメーション無効・時間倍率に従う | 状態・結果・操作可能性は動きなしでも伝わること |

今回の準備画面はButtonGroupによる自動／ライト／ダークの排他的選択を入口とする。
自動は端末設定に追従し、ライト／ダークを選んだ場合は端末設定の変更で選択を取り消さない。
チェック表示・標準の選択状態semanticsを使用し、幅が不足すれば標準overflow menuからも操作できるようにする。
表示切替は画面内の一時状態で、回転・resize時はSavedInstanceStateから復元する。設定ファイル・認証・通信・記録保存は追加しない。
準備中という理由で無期限のローディングや無効なログインボタンは置かない。

840dp以上かつ文字倍率1.5未満では、ブランド・説明と表示操作・対応範囲を左右に分ける。
それ以外はスクロールできる1列にし、文字2倍でも本文・操作を切り捨てない。レイアウトを切り替えても選択状態は保持する。

### 1.5系の部品を使う場所

[Material Design 3 UI/UXスキル](https://github.com/skydashnet/material-design-3-ui-skill/tree/a7d28f28251b64740b74dd0046971f23fbe74758)の
component semantics・semantic token・adaptive layout・accessibilityの指針を適用する。
透明な端末風の背景はブランド上の意図的な表現とし、操作自体はMaterialの標準動作を保つ。

| 部品・機能 | 現在の実装または接続する段階 |
|---|---|
| `MaterialExpressiveTheme` | 全画面のテーマ。人格色とブランド書体をsemantic roleで設定 |
| `ButtonGroup`／`toggleableItem` | 準備画面の排他的な表示選択。幅の押下反応・ToggleButtonの形状変化・overflowを標準実装へ委譲 |
| Expressive List | 対応範囲を非操作の`SegmentedListItem`と`ListItemDefaults.segmentedShapes`で表示。後続の記録一覧には操作可能なListItemを用途に応じて使用 |
| 強調Typography | 画面・節の見出しに`headlineLargeEmphasized`／`titleMediumEmphasized`。本文を一律に強調しない |
| `MotionScheme.expressive()` | ButtonGroup等の標準部品が参照。独自のばね定数や待機用アニメーションを重ねない |
| morphing Chip | C33の勝者絞り込み等で`FilterChip(shapes = FilterChipDefaults.shapes())`を使用する。C01には対象データがないためダミーのフィルターは作らない |

選択は即時反映し、追加の保存ボタンを置かない。未実装機能は「準備中」と記載する情報行であり、クリック可能なナビゲーションではない。

### 画面実装へ組み込む順序

見た目の決定をC34まで後回しにしない。既存PRの番号順は維持し、各画面の実装時から基本デザインを適用する。

| 段階 | デザイン作業 |
|---|---|
| 現段階 | 共通テーマ・書体・背景と準備画面を実装し、明暗・狭い幅を確認 |
| C16・C21の着手前 | ログインと記録一覧の画面モックで、情報配置・操作位置・主要な遷移を確認 |
| C16〜C25 | 画面実装へテーマを適用。loading・空・失敗・再試行・期限切れも同時に設計 |
| C34 | 実際に繰り返し使う部品を共通化し、全画面の色・書体・形を統一 |
| C35〜C36 | NEW／OLD・ログイン演出、Predictive Back、広い画面・文字拡大・TalkBackを仕上げる |

本文のコントラスト、48dp以上の操作領域、スクロール可能な長文、system barとの余白を各実装で守る。
横幅320dp・文字拡大で内容を切り捨てず、ダークモードでも装飾と文字を識別できるようにする。

### Material 3 Expressiveの全面採用

Material 3 `1.5.0-alpha28`をAndroid全画面の基盤とする。テーマだけの試験採用やstable版との使い分けは行わない。
依存するCompose本体もプレビュー版を採用し、Version Catalogで公式Alpha BOMとMaterial 3を固定する。

| 対象 | 採用版・管理方法 |
|---|---|
| Compose BOM | `androidx.compose:compose-bom-alpha:2026.09.00` |
| Material 3 | `1.5.0-alpha28`を明示固定。BOMの指定と一致させる |
| UI・Foundation・Runtime・Animation・Tooling | BOMが指定する`1.13.0-alpha03` |
| Compose Compiler | BOMの対象外。Kotlinと同じ版を維持する |

1.5系の公開APIを上記の用途へ接続し、非推奨の旧ListItemや旧ToggleButton overloadを使わない。
後続の認証・一覧・詳細もこのテーマと部品へ統一する。画面の採用方針であり、認証・通信・保存の安全仕様は変更しない。

Alpha BOMは安定版として保証される構成ではないため、API変更・描画・操作の回帰を更新時の確認対象とする。
更新時はBOMとMaterial 3を一組として扱い、依存解決・ビルド・Lintと明暗の代表画面を確認する。
破壊的変更で操作・アクセシビリティが維持できない場合は、依存の一部だけを下げず、直前に検証した組合せへ戻す。
独自の色・フォント・背景は維持する。

## 確認と今後の境界

C01／C02はWrapper経由のdebug APK生成とAndroid Lintを確認する。
C02では実Activityを使う2件のinstrumentation testで、graph接続・テーマ選択のイベント往復・Activity再生成後の選択復元を確認する。
ライブラリ内部や装飾の座標を写した自動テストは追加しない。
デザイン変更はエミュレーターで自動／明暗切替、320dp・文字2倍、840dp境界・広い幅、回転、アニメーション無効時を確認する。
ButtonGroupのoverflow menu、keyboardからの選択、読み上げ時の状態、配色のコントラストを確認する。
実機起動、認証、署名済み配布は後続段階で確認し、エミュレーターやビルド成功で代用しない。

### SSH環境の画面確認

RakuOS 44でEmulator 37.1.11／API 36を使う場合は、KVMと`-no-window -gpu swangle`を組み合わせる。
2026年9月16日にAndroid起動、準備画面の表示、明暗切替、スクリーンショット取得を確認した。
広い画面では2列表示とする。表示切替は文字の固有幅で計測し、収まらない選択肢を標準overflow menuへ移す。
均等幅への圧縮や文字サイズの縮小で、システムの文字拡大を妨げない。
`host`のEGL初期化失敗や従来のソフトウェア描画での異常終了は、SSHそのものの制約とは扱わない。
ホストのセキュリティ設定やGPUドライバーを変えず、AVD単位の描画方式で回避する。
AVDは永続領域へ保存し、再起動時も同じ構成を使う。起動・操作・終了手順はAndroid READMEを参照する。
ADBの接続先を明示し、制御ポートは外部公開しない。取得画像やUI dumpには実データを残さない。
ヘッドレス環境は表示・操作の確認に使い、実機の性能や読み上げの実聴を確認済みとは扱わない。

初回配布ではRecordsの既存認可を維持し、保存鍵のPQC処理にBouncy Castle、
端末内の秘密鍵保護にAndroid Keystoreを使用する計画とする。C01にはこれらの依存を追加しない。
HTTPSはOS標準TLSを使い、アプリ全体の暗号プロバイダーは置き換えない。

## 公式資料

2026年9月16日に確認。依存の具体的な版は実装の固定設定を正とする。

- [AGP 9.4の互換性](https://developer.android.com/build/releases/agp-9-4-0-release-notes)
- [built-in Kotlin](https://developer.android.com/build/migrate-to-built-in-kotlin)
- [Kotlin Gradle Pluginの版の指定](https://developer.android.com/build/releases/agp-9-0-0-release-notes#runtime-dependency-on-kotlin-gradle-plugin)
- [Gradle Wrapper](https://docs.gradle.org/9.6.0/userguide/gradle_wrapper.html)
- [Material 3と独自テーマ](https://developer.android.com/develop/ui/compose/designsystems/material3)
- [Compose Material 3リリース情報](https://developer.android.com/jetpack/androidx/releases/compose-material3)
- [Compose BOMとプレビュー版の管理](https://developer.android.com/develop/ui/compose/bom)
