# Records Android

C01のExpressive画面へC02のCircuit／Metroを接続し、C03のAndroid専用CIで確認している。
C16でC13のトークン保存、C14の認証APIクライアント、C15のAuth Tabを画面へ接続した。
ログイン・期限切れ・通信障害・ログアウトを区別し、一時的な明暗表示切替も維持する。
C17で記録1件の表示、C18でRelease署名、C19でApp Linksを接続した。C20でPlay内部テスト版の実機ログインと記録表示を確認した。C21〜C22では最近の記録を追加取得できる一覧にした。
C23では詳細の3人の初回意見・最終案を表示し、Markdown本文をMaterial 3対応のrendererで描画する。
C24では3人の投票・得票数と、記録に採点があれば展開式の内訳を表示する。勝者はAPIの保存結果を使う。
C25では親愛度と勝利コメントなどの任意項目を表示する。
C26〜C28でKeystore・PQC・Roomの暗号化保存部品を追加し、C29で認証済み記録を保存するようにした。C30は期限付きオフライン認可とログアウト時の記録削除、C31は全記録同期・キャンセル・再開、C32は保存済み一覧・詳細のオフライン表示とオンライン更新、C33はローカル検索・勝者絞り込み・並べ替えを担当する。
CodeQL接続（C04）はKotlin 2.4.20への対応待ちとする。Kotlinはダウングレードしない。

## 開発環境

- Android Studioではこのディレクトリを開く。
- JDKはTemurinを使用し、バージョンは`.java-version`に合わせる。
- Android SDK Platform 37.1（`platforms;android-37.1`）とBuild Tools 36.0.0を用意する。
  Composeのコンパイル要件に合わせたもので、minSdk 26・targetSdk 37は維持する。
- `JAVA_HOME`にJDK、`ANDROID_HOME`にSDKのディレクトリを指定する。
  Android Studioが作る`local.properties`でもSDKを指定できるが、Gitへ追加しない。
- Gradleは同梱Wrapperを使う。プラグイン・ライブラリは`gradle/libs.versions.toml`を正とする。

### Kotlin Compiler Native Image（単体CLI）

Kotlin 2.4.20の公式Native Image版を、単体ソースのコンパイルに利用する。
AGP／Gradleのコンパイラーは自動では切り替わらず、APK生成は既存方式のままとする。
通常のKotlin/JVMバイトコードを生成するもので、Kotlin/Nativeへの移行ではない。

[公式配布物](https://github.com/JetBrains/kotlin/releases/tag/v2.4.20)のうち、
Linux x86_64では`kotlin-native-image-linux-x86_64-2.4.20.tar.gz`を使用する。
SHA-256は`a249c9270ab8fed93f9d54756677b3fa3da652b80791227036231b5869d5d73f`。
照合後にツール用の永続ディレクトリへ展開し、そのルートを`KOTLIN_NATIVE_IMAGE_HOME`へ指定する。
`JAVA_HOME`は引き続き`.java-version`に合わせる。通常の`kotlinc`やGradle設定は上書きしない。

```sh
"$KOTLIN_NATIVE_IMAGE_HOME/bin/kotlinc-native-image.sh" -version
"$KOTLIN_NATIVE_IMAGE_HOME/bin/kotlinc-native-image.sh" Sample.kt -jvm-target 17 -d out
"$JAVA_HOME/bin/java" -cp "out:$KOTLIN_NATIVE_IMAGE_HOME/lib/kotlin-stdlib.jar" SampleKt
```

Linux版で単体Kotlinのコンパイル・実行と、Compose Compiler 2.4.20＋Compose Runtimeを渡す
最小Composableの変換を確認した。APK全体のNative Imageビルド成功を意味しない。
`-include-runtime`ではリソース参照エラーが発生したため、class出力と明示classpathを使用する。
配布物はExperimentalであり、上流の`Unsafe::invokeCleaner`警告も残っている。

## 確認

```sh
./gradlew :app:assembleDebug :app:lintDebug
```

APKは`app/build/outputs/apk/debug/app-debug.apk`に出力する。
debug版のapplication IDは`dev.pitekusu.shittim.records.dev`であり、配布版と分離する。
C02の接続確認は、専用エミュレーターまたはテスト端末で次を実行する。

```sh
./gradlew :app:connectedDebugAndroidTest
```

実Activityを起動する2件でMetro→Circuit→UIの接続、表示切替、Activity再生成後の復元を確認する。
C13の6件は実Android Keystoreで暗号化保存・再読込・改ざん／鍵消失の拒否・削除・書込失敗・
バックアップ除外を確認する。テスト専用のディレクトリと鍵を使い、既存の認証情報には触れない。
C14は型検証2件とMockEngineによる5件で認証APIの変換・失効／通信失敗・転送拒否・上限・キャンセルを確認し、本番へ通信しない。
装飾の座標やライブラリ内部を写す試験は追加しない。
CodeQL 2.27.0でのKotlin解析は未完了。通常ビルドの成功と混同しない。
見た目の変更はエミュレーターで自動／明暗切替、320dp・文字2倍、840dp境界・広い幅、回転、アニメーション無効時を確認する。
狭い幅のoverflow menuとkeyboard操作からも表示を選べることを確認する。
Previewやビルドの成功は、実機起動・Play配布の確認とは区別する。

### SSH接続先のLinuxで画面を確認する

RakuOS 44ではEmulator 37.1.11／API 36のAVDを、KVMと`swangle`で起動できることを確認した。
`host`はEGL初期化に失敗し、以前の`swiftshader`／`off`起動も異常終了したため、
この環境ではANGLE経由のソフトウェア描画を明示する。X転送やホストのGPU設定変更は不要。
ソフトウェア描画のため、実機の描画性能を評価する用途には使わない。

AVDは`shittim-expressive-preview`を使用する。別の環境ではDevice Managerまたは`avdmanager`で
同名のAPI 36 x86_64端末を用意し、AVDデータは一時ディレクトリではなく永続領域に保存する。
次の起動コマンドは専用ターミナルで実行し、既に起動済みなら重ねて実行しない。

```sh
"$ANDROID_HOME/emulator/emulator" -accel-check
"$ANDROID_HOME/emulator/emulator" -avd shittim-expressive-preview \
  -no-window -no-audio -no-boot-anim -no-snapshot -gpu swangle \
  -camera-back none -camera-front none -port 5580
```

別のターミナルから、起動完了値が`1`になってからインストールする。
以下は`apps/records-android`で実行する。接続先を明示し、実機や別AVDへ誤操作しない。

```sh
"$ANDROID_HOME/platform-tools/adb" -s emulator-5580 shell getprop sys.boot_completed
"$ANDROID_HOME/platform-tools/adb" -s emulator-5580 install -r app/build/outputs/apk/debug/app-debug.apk
"$ANDROID_HOME/platform-tools/adb" -s emulator-5580 shell am start -W \
  -n dev.pitekusu.shittim.records.dev/dev.pitekusu.shittim.records.MainActivity
"$ANDROID_HOME/platform-tools/adb" -s emulator-5580 exec-out screencap -p > /tmp/shittim-preview.png
```

操作は`adb -s emulator-5580 shell input`、画面要素の確認は`uiautomator dump`で行える。
ホスト側のデスクトップ画面は開かないが、スクリーンショットをCodex等で表示できる。
終了は`adb -s emulator-5580 emu kill`。ADBやエミュレーターの制御ポートは外部公開しない。
実データを含むスクリーンショットやUI dumpはGitへ追加せず、個人情報として扱う。
レビュー用画像は公開可能な準備画面だけを選ぶ。

### レビュー用スクリーンショット

C33の検索・勝者絞り込み・並べ替え部品（API 36・架空の選択状態のみ・実データなし）：

- [ライト](screenshots/search-light.png)
- [ダーク](screenshots/search-dark.png)
- [320dp・文字2倍](screenshots/search-large-text.png)

C32追加の自動同期の状態通知（API 36、架空の画面状態のみ・実データなし）。専用の同期メニュー・手動更新ボタンは設けず、同期中・失敗時だけ一覧へ小さく表示する：

- [ライト](screenshots/sync-light.png)
- [ダーク](screenshots/sync-dark.png)
- [320dp・文字2倍](screenshots/sync-large-text.png)

C16のログイン画面（API 36、未認証・実データなし）：

- [ライト](screenshots/login-light.png)
- [ダーク](screenshots/login-dark.png)

2026年9月17日、API 36のエミュレーターで取得。アカウント情報・実データは含まない。

- [通常表示](screenshots/bootstrap-default.png)
- [320dp・文字2倍：文字を縮めず標準メニューから選択](screenshots/bootstrap-large-text-menu.png)

## C03のCI

- 共通CIの`android-gate`でdebug APK・テストAPK・Lintを実行し、API 36のエミュレーター1台で画面・保存のinstrumentation testを確認する。
- JDKは`.java-version`、GradleはWrapperをローカルと共有する。CIにもアプリと同じSDK／Build Toolsを用意する。
- Android配下と関連文書だけの差分ではCoreの全pytest・パッケージ・CDK検証を省略する。
- `android-gate`は必要な処理の失敗・取消・skipを不合格にする。手動CIではAndroidも必ず検証する。
- Lint・テストのレポートを7日保存する。APK配布・CodeQL対応待ちのC04は含めない。

## C02の責務

- `MainActivity.kt`：Activity単位のMetro graphを生成し、CircuitContentで画面を表示。
- `RecordsGraph.kt`：Circuitへ準備画面のPresenterとUIを登録。未使用の依存やscopeは追加しない。
- `BootstrapPresenter.kt`：画面識別子・State・Eventと、一時的な表示選択の保持／復元。
- `BootstrapUi.kt`：Stateを受けて描画し、選択操作をeventSinkへ返す。Previewは固定Stateだけで表示。
- Circuit `0.39.0`、Metro `1.4.4`を固定。Kotlin `2.4.20`とMaterial 3 `1.5.0-alpha28`は維持。

## C18のRelease設定

- debug版は従来どおり`.dev`付きでビルドする。release版は本来のapplication IDを使用する。
- `shittimAndroidVersionCode`（正の整数、既定`1`）と`shittimAndroidVersionName`（`major.minor.patch`、既定`0.0.1`）をGradleプロパティで指定できる。更新時はPlayに提出済みのcodeより大きくする。
- 署名には環境変数`SHITTIM_ANDROID_UPLOAD_KEYSTORE`、`SHITTIM_ANDROID_UPLOAD_STORE_PASSWORD`、`SHITTIM_ANDROID_UPLOAD_KEY_ALIAS`、`SHITTIM_ANDROID_UPLOAD_KEY_PASSWORD`を使用する。秘密値をコマンド行、`gradle.properties`、Git、ログへ書かない。
- いずれかの入力またはkeystoreファイルが欠ければ、`assembleRelease`・`bundleRelease`は成果物作成前に失敗する。実際のupload keyの発行・管理、Play署名証明書とApp Linksの接続はC19以降の運用で扱う。
- 合成テスト鍵でのAAB署名確認は実施するが、配布版の署名や端末ログインの確認とは区別する。C39の配布Workflowは同一AABの検証・提出を別途実装する。

## C19のApp Links

- `https://shittim.pitekusu.dev/.well-known/assetlinks.json`はRecords Web Releaseが配信する。Play Consoleのアプリ署名鍵（従来鍵とポスト量子暗号鍵）を登録し、upload/debug鍵は登録しない。
- 固定認証callbackと`/records/{43文字のID}`だけがアプリのHTTPSリンク対象。記録リンクは固定host・query／fragmentなしを確認してから画面へ渡す。
- 未ログインで記録リンクを開いた場合はその記録をログイン後の復帰先にし、ログイン済みなら記録を再取得する。記録の公開範囲はAPIの認可で決まる。
- Play配布版でのOS検証、実Discordログイン、実upload keyで署名したAABの提出は、本番配布の準備ができた時点で行う。debug署名のエミュレーター確認はその代替にならない。

## C20：本人向け内部テスト

最初は本人だけを[Play Consoleの内部テストトラック](https://support.google.com/googleplay/android-developer/answer/9845334)へ登録する。
「内部アプリ共有」は別の鍵で再署名されるため、Playアプリ署名鍵を使うApp Linksの受入確認には使用しない。
友人への配布、一般公開、Android配布Workflowの自動化はまだ行わない。

1. Records Releaseで`https://shittim.pitekusu.dev/.well-known/assetlinks.json`を配信する。初回配信より前にReleaseIdentityの対象ファイル限定IAM変更を適用する。次の比較が成功し、Play Consoleの「アプリ署名鍵」に現在表示されるSHA-256がすべて同ファイルにあることを確認する。証明書が増えた場合は追加してから配信する。

   ```sh
   curl --fail --silent --show-error --max-time 15 \
     https://shittim.pitekusu.dev/.well-known/assetlinks.json \
     | cmp - ../records-web/public/.well-known/assetlinks.json
   ```

2. [Googleの案内](https://support.google.com/googleplay/android-developer/answer/9842756)に従い、本人がリポジトリ外へupload keyを作る。Playの「アプリ署名鍵」とは別物。鍵とパスワードはバックアップを取り、Git・チャット・CI artifactへ渡さない。以下は秘密鍵ファイルの保存先を自分で指定した後、対話的にパスワードを入力する例。

   ```sh
   umask 077
   # SHITTIM_ANDROID_UPLOAD_KEYSTORE にリポジトリ外の保存先を設定してから実行する
   keytool -genkeypair -keystore "$SHITTIM_ANDROID_UPLOAD_KEYSTORE" \
     -alias shittim-upload -keyalg RSA -keysize 4096 -validity 9125 \
     -storetype PKCS12
   ```

3. Play Consoleの「内部テスト」で本人のGoogleアカウントだけをテスターに追加する。提出済みの最大`versionCode`より大きい番号を選び、秘密値を対話入力して同じ端末で署名済みAABを作る。この手順で作るPKCS12では鍵パスワードに保管庫と同じ値を使う。パスワードをコマンド引数、`gradle.properties`、シェル履歴へ書かない。

   ```bash
   # 保管庫のパスワードを入力してEnter（入力内容は表示されない）
   read -r -s SHITTIM_ANDROID_UPLOAD_STORE_PASSWORD; printf '\n'
   SHITTIM_ANDROID_UPLOAD_KEY_PASSWORD=$SHITTIM_ANDROID_UPLOAD_STORE_PASSWORD
   export SHITTIM_ANDROID_UPLOAD_KEYSTORE SHITTIM_ANDROID_UPLOAD_STORE_PASSWORD
   export SHITTIM_ANDROID_UPLOAD_KEY_PASSWORD
   SHITTIM_ANDROID_UPLOAD_KEY_ALIAS=shittim-upload ./gradlew :app:bundleRelease \
     -PshittimAndroidVersionCode=1 -PshittimAndroidVersionName=0.0.1
   unset SHITTIM_ANDROID_UPLOAD_STORE_PASSWORD SHITTIM_ANDROID_UPLOAD_KEY_PASSWORD
   ```

   番号`1`と`0.0.1`は初回・未使用の場合の例。成果物は`app/build/outputs/bundle/release/app-release.aab`に作られる。Play Consoleの「内部テスト」→「リリースを作成」でこのAABを提出し、パッケージ名`dev.pitekusu.shittim.records`、版番号、配布対象が本人のみであることを確認して公開する。Playが配布用APKをアプリ署名鍵で署名するため、upload keyのSHA-256を`assetlinks.json`へ追加しない。

4. 本人の実機でテスター参加リンクからPlay版をインストールする。debug版や「内部アプリ共有」版で代用しない。Androidの設定で対象ドメインが「検証済み」か確認する。開発者向けADBが使える場合は、`adb shell pm get-app-links dev.pitekusu.shittim.records`の`shittim.pitekusu.dev: verified`でも確認できる。確認時に端末の既定アプリ設定を手動変更して検証成功を装わない。
5. 実Discordログイン後に記録1件が表示されること、ログアウト後に記録リンクを開いて再ログインすると同じ記録へ戻ることを確認する。callback URLの一回限りコードやBearer tokenをスクリーンショット・ログへ残さない。
6. 更新試験は同じupload keyで、より大きい`versionCode`のAABを内部テストへ提出し、Playから更新する。保存済みセッションと記録表示が壊れないことを確認する。失敗版を旧AABへダウングレードせず、新しい版番号の修正版で直す。

記録する受入結果は版番号、内部テストの状態、App Links検証、ログイン・記録復帰・更新の成否だけにする。署名鍵・パスワード・token・private Discord ID・実質問を記録しない。鍵が未作成、Play配布未実施、または実機未確認ならC20の配布受入は未完了として扱う。

## C17の記録1件表示

- `RecordsReadClient.kt`：既存の一覧から最新1件のIDを選び、詳細APIを取得。Ktor ContentNegotiationで必要な表示項目だけ変換する。
- `RecordPreviewPanel.kt`：読み込み・空・エラー・議題／勝者／結論を表示。本文はメモリ内の画面状態だけで保持する。
- `MobileSessionModel.withAuthorizedToken`：保存tokenをリクエスト中だけ貸し、ログアウト・期限切れ・アカウント切替後に戻る結果を捨てる。
- C21以降、通常起動では一覧を表示し、カード選択か個別記録リンクからこの1件表示を開く。
- 架空APIとエミュレーターで画面・境界条件を確認する。C19の実署名ログインはこの工程の検証には含めない。

## C21の議論一覧

- `RecordsReadClient.recentRecords`は既存の認証済みAPIから12件ずつ取得し、議題の要約・依頼者・完了日・勝者を画面用データへ変換する。C22ではPagingの`PagingSource`が次のcursorを渡し、必要なページだけ追加取得する。
- `RecordListPanel`はカード、読み込み・空・失敗・再試行を表示する。カードを選ぶとC17の1件表示を開き、画面内ボタン・システムBackで一覧へ戻れる。一覧のスクロール位置は維持する。
- 依頼者アイコンはCoilの`AsyncImage`を利用する。署名付き画像のdisk cacheは無効化し、認証状態から離れた際にmemory cacheを消去する。tokenや質問本文は画像リクエストへ渡さない。
- `BootstrapPresenter`は一覧と選択した1件を、現在の認証セッションにだけ結び付ける。選択先は既存の認証モデルで管理し、画面復帰・回転後も維持する。詳細への往復では取得済み一覧を保持し、認証更新では再取得する。一覧へ戻ると選択先を解除し、処理済みApp Linkは再適用しない。ログアウト後に前ユーザーの記録を残さない。

## C22の一覧ページング

- 一覧は`LazyColumn`とPaging Composeで遅延描画する。追加ページの通信失敗は取得済みカードを残して再試行できる。
- APIのcursorが期限切れになった場合は、表示された操作から最初のページを読み直す。ページ間の重複IDは表示しない。
- 全件を一括取得せず、永続キャッシュも追加しない。詳細へ移動して戻る間は一覧のスクロール位置を保持する。

## C23の議論本文

- 詳細APIから3人の初回意見と最終案を読み、人格の対応と必須本文を検証して表示する。投票・評価・親愛度は後続工程で扱う。
- Markdownの構文解析とMaterial 3描画は`multiplatform-markdown-renderer`に任せる。外部リンクは絶対HTTPS URLだけを開き、Markdown画像のURLは自動取得しない。
- 本文は既存の認証済み画面状態でのみ保持し、永続キャッシュ・ログ・テレメトリーへ追加しない。

## C24の投票・評価

- 詳細APIの投票者・投票先・理由・得票数を検証して表示する。投票の重複・自己投票・得票数と保存済み勝者の不整合は不正応答として拒否し、アプリで勝者を選び直さない。
- 新方式では5項目の採点と総合点・理由を投票ごとに展開できる。投票先がその投票者の最高採点候補であり、投票理由が選択候補の採点理由と一致すること、同票時の採点・勝者・決定方式の整合を検証するが、勝者を選び直さない。理由の500文字上限はUnicode文字数で判定する。採点がない旧記録でも投票を表示し、APIが示す決定方式または旧同票表示を使う。
- 記録内容を新たに端末へ永続保存しない。

## C25の親愛度・任意項目

- 旧記録の`affection: null`は数値を作らず「親愛度データなし」と表示する。`unavailable`は評価不能の説明と実増減0を示す。
- `applied`では3人それぞれの変更前・変更後と実増減を表示する。質問評価点は画面に表示しないが、上限・下限で実増減と異なる場合もAPIの値を検証する。
- 勝利コメント、実行案、注意点は内容がある場合だけ表示する。欠損・重複・範囲外・数値の不整合を拒否し、親愛度は変更しない。

## C26の保存用秘密鍵保護

- `storage/KeystorePrivateKeyStore.kt`は、後続のML-KEM秘密鍵バイト列をAndroid Keystoreの非書出しAES-256-GCM鍵で暗号化し、`noBackupFilesDir`へ`AtomicFile`で保存する。
- v1形式・長さ上限・アカウント識別子を含むAADを検証し、異なる秘密鍵、別アカウント、改ざん、鍵消失で上書きしない。例外には秘密値を含めず、平文への切替もしない。
- 初回の書込が確定せず本体ファイルもない失敗では新規鍵を片付ける。既存ファイルがある場合は鍵を残し、暗号文を無効化しない。
- `clear`は専用の鍵とファイルだけを削除する。読み出したbyte配列は呼出元が消去する。Keystore操作はUI thread外から呼ぶ。
- Roomへの接続はC28で行い、認証と記録Repositoryの接続はC29〜C30で行う。この工程では実際の議論記録の永続保存やログアウト時の記録削除をまだ行わない。

## C27のデータ鍵保護

- Bouncy Castle 1.86のHPKE（ML-KEM-768／HKDF-SHA256／AES-256-GCM）で、32-byteの記録データ鍵を包む。KEM・鍵導出・AEADを自作しない。
- ML-KEM秘密seedはC26のKeystore保護に保存する。wrapの形式はversion、encapsulation、認証付き暗号文。アカウント識別子と記録IDをAADに結び付ける。
- unwrapは鍵が欠損しても再作成しない。改ざん・別アカウント・別記録・形式不正を拒否し、秘密値や詳細な暗号例外を出さない。
- C27はデータ鍵保護部品のみ。byteペイロードの暗号化とRoom保存はC28、実際の議論モデルとの接続はC29で行う。

## C28の暗号化済み記録保存

- Room 3.0.3とKSPでschemaを管理し、Android標準SQLite driverを使う。schema JSONもレビュー対象として保存する。
- 一覧・詳細のbyteペイロードをランダムなデータ鍵でAES-256-GCM暗号化し、C27で鍵を包んでからRoomへ保存する。質問・依頼者名・回答は平文列へ書かない。
- アカウント・記録・一覧／詳細の種別をAADで束縛する。破損・別アカウント・鍵欠損は固定の失敗分類で拒否し、平文fallbackを設けない。削除APIは暗号文の行のみを対象とする。
- C28は未接続の保存部品であり、実ユーザーの記録はまだ端末へ保存されない。Repositoryへの接続とログアウト時の鍵失効はC29以降で扱う。

## C29の記録Repository接続

- 認証済みモバイルセッションの`cacheAccountId`をキャッシュの本人識別に使用する。表示名やBearer tokenは識別子にしない。サーバー側の認証応答を先に配信してから、このフィールドを必要とするAndroid版を配布する。
- 検証済みの一覧項目と詳細を`RecordsRepository`がバージョン付きJSONへ変換し、C28の暗号化Storeだけへ保存する。保存失敗時は`STORAGE_UNAVAILABLE`で止め、平文表示へ切り替えない。
- 再起動後の暗号化済み読込口を用意するが、画面にはまだオフラインデータを表示しない。明示ログアウト直後の削除と90日の認可はC30、同期・表示統合はC31〜C32で接続する。
- アカウント切替でC26の単一鍵が次の利用者の保存を妨げないよう、保存開始前に旧鍵を失効させ、旧暗号文を全削除する最小の切替処理をC29へ前倒しした。明示ログアウト直後の削除とオフライン認可は引き続きC30で扱う。

## C30のオフライン認可とログアウト

- session APIで本人を確認してから、`cacheAccountId`・確認時刻・絶対期限をtokenと同じKeystore暗号文へ保存する。保存形式v2は既存v1も読み、v1だけではオフライン閲覧を許可しない。氏名・アイコン・質問は認可情報へ保存しない。
- キャッシュの読取口は本人一致と期限を前後で確認する。期限は保存済みtoken・サーバー応答・確認から90日のうち最短とし、通常利用や再確認では延長しない。有効な保存済み許可はsession応答待ちでも利用でき、通信障害／5xx時も維持する。未検証token・認証拒否では利用せず、401／403では即座にロックする。プロフィールや通常APIへのtoken使用はサーバー確認まで復元しない。
- 期限切れはtokenを削除して記録をロックするが、記録の鍵・暗号文は保持する。同じアカウントで再認証すれば再利用できる。
- 明示ログアウトでは直ちに読取を閉じ、記録鍵を失効→Roomの暗号文を全削除→所有者マーカーを削除→tokenを削除する。ネットワークや画面終了に依存せず端末削除を先に完了させ、サーバー失効POSTは従来どおり1回だけ試す。端末削除失敗は保存エラーとして再ログインを止め、明示削除の再試行を可能にする。
- 別アカウントへの切替はsession確認直後に旧鍵・暗号文を消す。遅れて完了した旧リクエストの保存と同一Mutexで直列化し、旧ユーザーのキャッシュを残さない。
- 削除前に個人情報を含まない削除予定マーカーを保存し、中断・失敗後は次の認証前に削除を再開する。マーカーが残る間はキャッシュの復号を許可しない。
- token側にも独立したログアウト予定を保存し、プロセス終了後はsession確認ではなく端末削除から再開する。tokenが既に消えていても、記録とtoken・専用鍵の削除がすべて完了するまで予定を消さず、再ログインを拒否する。端末削除失敗の再試行用に旧tokenをメモリだけで保持し、削除後の失効POST前に再送用の参照を破棄する。
- オフラインの画面表示・全記録同期はC31〜C32で接続する。オフライン中のサーバー失効は再接続まで検知できない。端末時計を使う期限確認に加え、同一プロセスでは単調時計のタイマーで期限を閉じる。OS自体の侵害や時計の意図的操作を完全に防ぐ仕組みではない。

## C31の全記録同期

- ログイン・復帰時に自動同期する。専用の同期メニュー・手動更新ボタンは設けず、同期中・失敗時だけ小さく状態を表示する。全索引ページをたどり、未保存・更新分だけを1件ずつ暗号化保存する。件数未確定のため推測した割合は表示しない。
- C31の初期実装は画面起点だったが、C32追加仕様でWorkManagerによるバックグラウンド自動同期へ変更した。詳細への移動・画面停止／再生成では同期を止めない。OSによる停止時は保存済み進捗から再開する。
- 進捗は既存の暗号化Storeに別用途で保存し、Room schemaや既存本文の形式は変えない。ログアウト・別アカウントへの切替では進捗も消す。token・質問・プロフィールは進捗へ保存しない。
- 再起動後もWorkManagerから未完了分を続行する。cursor期限切れは1回だけ先頭から列挙し直し、版が一致する保存済み詳細は再取得しない。通信／保存エラーやキャンセルでは完了扱いにしない。
- 初回保存後は軽量な同期索引を照合し、新規・更新分だけ取得する。C32では全ページ成功後の不在候補を詳細APIで確認し、404の場合だけ削除する。保存済み詳細は開くたびに再取得しない。

## C32の保存済み表示と更新

API 36・架空データで確認した保存済み詳細パネル：[ライト](screenshots/offline-light.png)／[ダーク](screenshots/offline-dark.png)／[320dp・文字2倍](screenshots/offline-large-text.png)。長い画面はスクロールして閲覧する。

- 前回の認可期限内なら、通信できない場合も保存済み一覧・詳細を閲覧できる。未取得の詳細は未保存と表示する。期限切れはロックし、同一アカウントの再認証で再び閲覧できる。ログアウト・切替は鍵・記録・進捗を消去する。
- 一覧・保存済み詳細はローカルから表示し、同期Workerが更新を反映する。通信・端末保存失敗では既存の内容を残す。依頼者アイコンも暗号化保存し、オフラインでは復号した画像をCoilへ渡す。画像未保存・欠損は代替表示とし、古い署名付きURLへ接続しない。
- 起動・復帰時は有効な保存済み認可を先に読み、通信完了を待たず一覧・詳細を表示する。同期進捗によるローカル再読込は直列・最新要求の集約で扱い、読み込みを繰り返し中断しない。同一アカウントの再確認中も表示を保つが、認証拒否・期限切れ・ログアウト・切替では直ちに隠す。
- 同一アカウントの保存済み一覧は同じPagingのFlowへ更新を流し、同期進捗で配信元を作り直さない。カードの記録IDをkeyとして、新規記録の追加や同期完了時も読んでいる位置を維持する。
- 認証済み404は一覧・詳細を同時に削除する。同期開始時の削除候補を暗号化進捗へ保存し、全ページ成功後の不在候補だけ詳細APIで確認する。索引の反映遅れだけでは削除せず、200なら保存済み内容を維持、通信失敗・キャンセルなら確認を再開する。候補以外の本文取得は増やさない。C31の旧進捗は先頭から安全に再列挙する。既存Room schema・暗号形式は変えない。
- Play内部テスト：オンラインで自動保存を完了 → 機内モードで再起動し一覧・詳細・アイコンを確認 → 再接続して差分更新 → ログアウト後は一覧・詳細が表示されないことを確認する。端末内のデータ削除は不要。
- 架空APIと実Keystore／Roomでページ重複・途中再開・期限切れ・巡回・404・認可喪失を確認する。実機試験とPlay配布は別途行う。

## C33：保存済み記録の検索・絞り込み・並べ替え

- 議題・依頼者・保存済み本文を端末内で検索する。空白区切りは全語一致、全半角・英字の大小文字は区別しない。未保存の本文を検索のためにダウンロードしない。
- 勝者は「すべて」または3人の1人で絞り、完了日時の新しい順／古い順へ切り替えられる。同日時は記録IDで順序を安定させる。
- 検索中・表示件数／保存件数・一致なしを表示する。同期中は現在保存されている分だけが対象となり、完成した記録は自動反映する。
- 入力と条件は同一アカウントの画面内メモリだけで保持し、詳細から戻る間は維持する。認可喪失・ログアウト・切替・画面再生成で初期値へ戻し、SavedStateや検索用平文DBへ保存しない。
- Material 3 Expressiveの形状変化する勝者チップと標準ButtonGroupを使用し、文字2倍では折り返し／overflowで操作できる。条件解除は入力・勝者・並べ替えをまとめて初期値へ戻す。
- 架空データの試験で本文検索・絞り込み・順序・通信なし・認可喪失・平文非保存と画面操作を確認する。API・Room schema・暗号形式・依存バージョンは変更しない。

### バックグラウンド自動同期と差分取得

- ログイン・アプリ復帰後に即時同期を予約し、約15分間隔の定期同期を一意なWorkManagerジョブとして保持する。ネットワーク接続が必要で、省電力・Doze・強制停止等により実行は遅れる。サーバーpushによる瞬時反映ではない。一覧・保存済み詳細に手動同期操作は置かず、通常の閲覧を妨げない。
- `GET /api/v1/records/sync-index`は50件ずつの軽量なID・記録版・アイコン版だけを返す。質問・依頼者名・回答は返さない。全索引を照合するため、古い日付で後から追加された記録や削除も検出できる。本文は未保存・更新版だけ取得し、同じ依頼者の同じ版のアイコンは共有して再取得しない。
- 同期索引をまだ持たない旧キャッシュは、初回だけ本文を再確認して基準版を作る。その間も保存済み記録は読める。新APIをRecords Releaseで先に配信し、その後Androidを配布する。旧アプリの既存GET契約は維持する。
- Workerは実行時にKeystoreからtokenを読み、サーバーの本人・期限を確認する。WorkManagerの入力・出力・進捗へtoken、質問、利用者IDを入れない。各保存境界で期限・削除意図・同じtoken・所有者を確認し、所有者を再活性化しない。失効確認時はオフライン認可もロックする。
- 8分で区切り、途中進捗から再開する。通信失敗は指数backoffで最大3回再試行する。通常の画面停止では継続するが、ログアウト・切替では予約を止め、鍵・暗号文・アイコン・進捗を消す。
- 実機確認：自動同期の完了後に機内モードで再起動し、アイコンと本文を確認。再接続後の新規記録追加と差分更新、画面を閉じた場合の継続、ログアウト後の非表示を確認する。

## C34：共通デザインシステム

- `ui/ShittimComponents.kt`に、主要パネル・関連情報の面・節見出し・人格名・読み込み表示・余白をまとめる。色・書体・形状・MotionSchemeは既存の`ShittimTheme.kt`を使い、新しいライブラリや汎用UI層は追加しない。
- `ShittimBackdrop`のSurfaceから本文色を供給する。背景上の見出し・空状態・読み込みメッセージもダーク表示で黒へ戻らない。
- ログイン・再試行と、ログアウト・端末内情報削除の操作は視覚的に分ける。人格名と勝者はシアン／ピンク／ラベンダーを名前付きで表示し、代替アイコンも同じ色の役割へ合わせる。
- LINE Seed JP／Delogyと利用条件は維持。Delogyは固定の短い英字ラベルのみ。素材・依存バージョン・認証・同期・保存形式は変更しない。NEW／OLDやログイン演出はC35へ残す。
- 架空記録による明暗・320dp／文字2倍・操作確認と背景本文色の回帰確認を行う。既存の採点内訳・親愛度・再試行・ログアウト・スクロール維持も確認する。実機ログインや配布の代替ではない。

### C34の画面写真

試験用の架空記録を表示した一覧・詳細のスクロール途中の画面。実利用者の記録や認証情報は含めない。

![一覧・ライト](screenshots/design-records-light.png)
![一覧・ダーク](screenshots/design-records-dark.png)
![320dp・文字2倍](screenshots/design-large-text.png)
![議論詳細・ダーク](screenshots/design-detail-dark.png)

## C35：NEW／OLD切替とログイン完了演出

- NEW＝新しい順、OLD＝古い順。標準ButtonGroup／ToggleButtonの接続形状と幅変化、MotionSchemeによる選択位置の移動を使用する。日本語の説明は文字拡大時に折り返し、選択状態は読み上げでも伝える。
- ログイン完了は、ブラウザーから戻っただけでは出さない。保存tokenをサーバーで確認できた対話的ログインの後だけ、チェック付きの標準Snackbarを約2秒表示する。アクセシビリティの推奨時間を優先する。
- 通常起動・アプリ復帰・回転では再演しない。全画面の待機や一覧への項目追加を行わず、表示中も記録やログアウトを操作できる。システムのアニメーション無効設定を尊重する。
- API・認可・暗号化保存・依存バージョンは変更しない。C36以降の仕上げ・配布は別工程とする。

この節はC35時点の実装記録。後続のブランド演出で完了Snackbarを置き換えた。

### C35の画面写真

API 36・架空データによる画面確認。実DiscordログインやPlay配布の確認ではない。

![NEW・ライト](screenshots/motion-order-light.png)
![OLD・ダーク](screenshots/motion-order-dark.png)
![320dp・文字2倍](screenshots/motion-order-large-text.png)
![ログイン完了・ダーク](screenshots/motion-login-complete.png)

## C36：可変幅レイアウトと画面操作

- Material 3 Adaptiveの標準一覧／詳細配置を利用。幅840dp以上・文字倍率1.5未満は左右2ペイン、それ以外は選択先を1ペインで表示する。詳細の本文幅は760dpまでとし、長文でも「一覧に戻る」を常時操作できる。
- リサイズや詳細往復で選択・検索条件・一覧スクロールを維持する。認可を失えば両ペインを直ちに取り除く。認証・API・同期・保存形式は変更しない。
- 戻るgestureで標準ペイン遷移をプレビューし、確定時だけ一覧へ戻る。キャンセルでは詳細を開いたままにし、認可喪失や別記録への切替後に古いgestureを確定しない。通常の戻るボタンとアニメーション無効時も同じ選択・スクロールを維持する。
- 日本語のpane名・見出し・表示中の選択状態をsemanticsへ設定し、装飾アイコンの代替イニシャルを重複して読ませない。文字拡大時は1列へ戻して折り返す。TalkBack実聴と実機・Play配布はエミュレーターのsemantics確認と区別する。

### C36の画面写真

API 36・架空データ。標準のテスト用window／font scale overrideで広幅と320dp・文字2倍を確認し、アニメーション倍率0／1で戻るの取消・確定も確認する。実機のTalkBack・Discord認証・Play配布の証拠ではない。

![一覧／詳細・広幅ライト](screenshots/adaptive-wide-light.png)
![一覧／詳細・広幅ダーク](screenshots/adaptive-wide-dark.png)
![一覧・スマートフォン](screenshots/adaptive-compact-dark.png)
![常時操作できる戻る・320dp文字2倍](screenshots/adaptive-large-text.png)

## C37：Web機能メニューとログアウト

- 認証済みの一覧・詳細の上部へメニューを追加。いろいろな記録、モモトーク、メモリアルロビー、サービス状態確認、プロンプト管理を固定URLでCustom Tabsへ開く。Web側の認可を使い、アプリのtokenはブラウザーへ渡さない。
- ログアウト・自動／ライト／ダーク切替をメニューへ移し、記録一覧にあったログイン状態カードと表示切替を撤去。通信不調によるオフライン閲覧でも同じログアウト操作が可能。
- ログイン前の常設説明文を削除。期限切れ・取消・失敗・ブラウザー不可の状態表示は残す。C35の短い完了通知は次の起動／ログイン演出PRで置き換える。
- 架空データの画面試験でメニューの導線とログアウト操作、一覧・詳細のスクロール維持を確認する。実Webへの接続とブラウザーからの復帰は内部テスト版で確認する。

## 起動・ログインのブランド演出

- Android標準SplashScreenから全画面のシッテム演出につなぐ。起動は約1秒、対話的ログインが確認・保存まで成功したときは約1.5秒。認可確認や保存済み記録の表示は演出と並行して進む。
- ロゴ円弧を回転させ、ログイン前と記録一覧のヘッダーでも約8秒で一周する。タップ・戻る操作で演出をスキップでき、アニメーション無効なら即時に本画面を表示する。回転や通常の画面復帰では再演しない。
- 取消・失敗時にはログイン成功演出を出さない。C35の完了Snackbarを置き換え、保存token・本文・利用者名を演出状態へ保持しない。
- API 36の未認証・架空環境で確認した[ログイン画面](screenshots/login-refined.png)と[起動演出の短い録画](screenshots/brand-intro.mp4)。実認証や実機性能の証拠ではない。

## 人格アイコン

- 既存WebPを同梱し、一覧の勝者・絞り込み・意見・詳細の勝者へ顔アイコンと名前を表示する。勝者は王冠で区別する。
- 表示モデルにAPIの人格slotを残す。旧キャッシュは既知の人格名から補完し、不明なら汎用アイコンにする。暗号化保存の移行や全件再取得は不要。

## 議論詳細の4画面

詳細は下部の結果・意見・投票・親愛度と左右スワイプで切り替える。ページごとの読位置を保持し、同期で選択ページを戻さない。新しい記録は結果から表示する。選択中かつ停止しているページだけで演出を許可し、メニュー表示中・画面非表示中には停止する。標準のShortNavigationBar／HorizontalPagerを使用し、APIと保存形式は変更しない。

議題は2行のヘッダーから全文シートを開ける。結果は王冠付きの勝者と結論を主役に、勝利コメントと件数付きの実行案・注意点を表示する。実行案・注意点は展開でき、本文を削らない。演出完了を閲覧条件にせず、戻る操作では先にシートを閉じる。

## 投票の表示

- 3人の顔を三角形に置き、投票先へ曲線矢印を順に描く。図が画面に完全に入ったら約1.8秒で描画し、画面内にある間は完成形を約0.9秒保って繰り返す。画面外・理由シート表示中・アニメーション無効時は完成形で静止する。得票数・勝者の王冠を併記し、顔を押すと投票理由と保存された5項目評価をBottom Sheetで開く。
- 文字拡大・狭幅では顔付きの3行表示へ切り替える。旧記録に採点がなければ理由だけを表示し、勝者を再計算しない。
- 投票の矢印は表示中に繰り返すため、演出済み状態を保持しない。演出のために本文をSavedStateへ入れない。
- 架空の循環投票による[関係図](screenshots/voting-graph.png)と[理由シート](screenshots/voting-detail.png)。

## 親愛度の表示

- 3人の顔・名前と10個のハート、変更前後の正確な点数、実際の増減を示す。質問評価点はカードに出さない。100点につきハート1個とし、端数は数字で確認できる。
- 広幅は横3枚、スマートフォンや文字拡大時は縦3枚。各カードが画面内へ完全に入った際に数値とハートを約1.1秒で一度だけ動かし、記録・カード単位で再演を抑える。画面内に収まりきらないカードとアニメーション無効時は最終値を即時表示する。
- 上限・下限により質問評価と実増減が異なっても、表示する実増減には保存値を使う。増減0、評価不能、親愛度のない旧記録に架空の値を補わない。
- 架空の上限・減少・増減0を並べた[親愛度カード](screenshots/affection-cards.png)。

## C16の認証画面

- `auth/MobileSessionModel.kt`：Activity再生成をまたぐ認証の寿命。保存tokenとsession APIを照合し、期限・失効・通信障害を区別する。
- `MainActivity.kt`／`RecordsGraph.kt`：既存AndroidX ViewModelをMetroへ注入。新規依存やRepository層は追加しない。
- `BootstrapPresenter.kt`：Circuitの表示状態・イベントと、C15のActivity Resultを接続する。
- `SessionPanel.kt`：ログイン前や認証不調時の説明と操作。tokenは受け取らず、閲覧中のログアウトはC37メニューへ移した。
- ログアウトは端末削除を先に行い、サーバー失効は1回だけ試す。応答不明では端末削除済み／サーバー失効未確認を明示する。
- 架空APIの状態遷移と画面操作をinstrumentation testで確認する。本番通信は行わず、実署名によるDiscordログインはC19に残す。

## C13のトークン保存

- `auth/StoredToken.kt`：43文字のBearer tokenと秒単位の絶対期限、C30からは任意の検証済みキャッシュ認可を保持する。`toString()`は常に伏せる。
- `auth/KeystoreTokenStore.kt`：`save`／`read`／`clear`。Android KeystoreのAES-256-GCM鍵を使用し、`noBackupFilesDir`へ暗号文だけを原子的に保存する。
- 鍵・保存形式はモバイル認証専用。鍵は書き出さず、IVは暗号化ごとに生成する。バックアップ・端末転送を禁止する既存Manifest／XMLも維持する。
- 破損・鍵消失・書込失敗は固定例外で通知し、平文保存へ切り替えない。鍵を失った保存データは明示的に`clear`してから再ログインする。
- `clear`は専用鍵と保存ファイルだけを削除する。サーバーでの失効は別処理であり、C14以降から接続する。
- ファイル／Keystore操作は同期処理のためUI thread外から呼ぶ。単一process内の複数インスタンスを直列化する。
- 有効期限の延長・認可判定・プロフィール保存は行わない。ログイン画面・通信・PQCによる記録キャッシュはこの工程へ含めない。

## C14の認証APIクライアント

- `auth/MobileAuthModels.kt`：既存モバイルAPIに対応する要求・応答。S256、取引ID、復帰先、Bearer、日時を検証する。
- DTOは通常classとし、文字列化で認証情報・本人情報を表示しない。
- Ktor 3.6.0＋OkHttp engine、kotlinx.serialization 1.11.0、Coroutines 1.11.0を固定。compiler pluginはKotlinと同じ版を使用する。
- `auth/MobileAuthClient.kt`：固定HTTPS originへ開始・交換・session・logoutを送るsuspend API。渡したengineを所有し、利用終了時に`close`する。
- native通信にはCookieを使わず、Bearerはsession／logoutにだけ付ける。TLSの標準検証・平文HTTP禁止を維持し、HTTP loggingやdisk cacheを持たせない。
- 接続10秒、request／socket 20秒、JSON応答64 KiB。redirect・接続復旧retryを無効化し、POSTはone-shot bodyとする。
- 401・grant不正・通信失敗・サーバー障害等は固定分類で返し、応答本文や原因例外を表示しない。キャンセルはそのまま伝播する。
- クライアントだけでは保存tokenの消去・延長・再認証を行わない。ブラウザー接続はC15、ログイン画面との接続はC16へ分離する。

## C15のブラウザーログイン

- `auth/MobileLoginActivity.kt`：AndroidX Browser 1.10.0のAuth Tabを起動し、Activity Resultで復帰・取消を受ける。`onResume`から取消を推測しない。
- 対応していないブラウザーではAuth TabのCustom Tabs fallbackを使い、公開するのは固定HTTPS callback専用のreceiverだけとする。
  両方の復帰経路を同じ検証へ渡し、通常の画面復帰や二重callbackで交換を繰り返さない。
- HTTPSのAuth TabにはDigital Asset Linksが必要。検証失敗・時間切れは拒否し、検証を迂回するブラウザーへ切り替えない。
  C19で実際のPlay署名証明書を設定するまでは、本番ブラウザー認証の完了を保証しない。
- `auth/MobileLoginFlow.kt`：既存の独自APIに合わせ、S256、独立したランダムstate、取引ID、固定callback、期限、一回限り交換を検証する。
  verifier／state／codeはメモリ内だけに保持する。process消失・取消・期限切れの復帰は、新しい認証取引を作らず拒否する。
- `auth/MobileLoginModel.kt`：回転をまたぐ進捗と交換・Keystore保存を管理。保存はUI thread外で行い、保存失敗をログイン成功にしない。
- `auth/MobileLoginContract.kt`：呼出元へは結果分類と元の目的画面だけを返し、tokenや復帰URLは渡さない。ログイン画面への接続はC16で行う。
- テストは合成データとMockEngineを使い、Auth Tab結果、fallback、取消、不正・古いcallback、二重交換、保存失敗を確認する。
  本番Discordや既存の保存tokenには触れない。端末のブラウザーと署名証明書による疎通確認はC19の配布確認で実施する。

## デザイン基盤

- `ui/ShittimTheme.kt`：surface／inverse／fixed色を含む意味別の配色、LINE Seed JP、Delogy、形状、Expressiveモーション。
- `ui/ShittimBackdrop.kt`：画像素材やblurを使わないグリッド・円弧・菱形。
- `BootstrapUi.kt`：幅・文字倍率に応じた1列／2列配置とExpressive List。未実装機能は押せるリンクに見せない。
- `BootstrapThemeSelector.kt`：ButtonGroupによる排他的な自動／明暗選択。均等幅を強制せず、文字が収まらない項目は標準overflow menuへ移す。押下時の幅・形状変化はMaterialのMotionSchemeを使用する。
- morphing Chipは実際の記録フィルターを作る段階で使用する。準備画面にダミーのフィルターや未使用の共通部品は置かない。
- Material 3 `1.5.0-alpha28`を全面採用し、Compose本体も`compose-bom-alpha:2026.09.00`で揃える。
  UI・Foundation・Runtime・Animation・Toolingは`1.13.0-alpha03`を使用する。
  stable版との混在を前提にせず、更新時はBOMとMaterial 3を一組として依存解決・ビルド・表示を確認する。
- 使用フォントの出典とライセンスは`app/src/main/assets/licenses/`を参照する。

設計と実装範囲は[Androidアプリ設計](../../docs/29_Androidアプリ設計.md)を参照する。
リファクタリングには指定の[Material Design 3 UI/UXスキル](https://github.com/skydashnet/material-design-3-ui-skill/tree/a7d28f28251b64740b74dd0046971f23fbe74758)を適用した。
