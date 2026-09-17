# Records Android

C01の最小Composeアプリと、先行するMaterial 3 Expressiveデザイン基盤。
現在は準備画面と一時的な明暗表示切替のみで、認証・通信・記録保存は行わない。
Circuit／Metro、CI、署名済み配布は後続コミットの対象とする。

## 開発環境

- Android Studioではこのディレクトリを開く。
- JDKはTemurinを使用し、バージョンは`.java-version`に合わせる。
- Android SDK Platform 37.1（`platforms;android-37.1`）とBuild Tools 36.0.0を用意する。
  Composeのコンパイル要件に合わせたもので、minSdk 26・targetSdk 37は維持する。
- `JAVA_HOME`にJDK、`ANDROID_HOME`にSDKのディレクトリを指定する。
  Android Studioが作る`local.properties`でもSDKを指定できるが、Gitへ追加しない。
- Gradleは同梱Wrapperを使う。プラグイン・ライブラリは`gradle/libs.versions.toml`を正とする。

## 確認

```sh
./gradlew :app:assembleDebug :app:lintDebug
```

APKは`app/build/outputs/apk/debug/app-debug.apk`に出力する。
debug版のapplication IDは`dev.pitekusu.shittim.records.dev`であり、配布版と分離する。
この段階では新しい自動テスト基盤を追加しない。
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

2026年9月17日、API 36のエミュレーターで取得。アカウント情報・実データは含まない。

- [通常表示](screenshots/bootstrap-default.png)
- [320dp・文字2倍：文字を縮めず標準メニューから選択](screenshots/bootstrap-large-text-menu.png)

## デザイン基盤

- `ui/ShittimTheme.kt`：surface／inverse／fixed色を含む意味別の配色、LINE Seed JP、Delogy、形状、Expressiveモーション。
- `ui/ShittimBackdrop.kt`：画像素材やblurを使わないグリッド・円弧・菱形。
- `BootstrapScreen.kt`：幅・文字倍率に応じた1列／2列配置とExpressive List。未実装機能は押せるリンクに見せない。
- `BootstrapThemeSelector.kt`：ButtonGroupによる排他的な自動／明暗選択。均等幅を強制せず、文字が収まらない項目は標準overflow menuへ移す。押下時の幅・形状変化はMaterialのMotionSchemeを使用する。
- morphing Chipは実際の記録フィルターを作る段階で使用する。準備画面にダミーのフィルターや未使用の共通部品は置かない。
- Material 3 `1.5.0-alpha28`を全面採用し、Compose本体も`compose-bom-alpha:2026.09.00`で揃える。
  UI・Foundation・Runtime・Animation・Toolingは`1.13.0-alpha03`を使用する。
  stable版との混在を前提にせず、更新時はBOMとMaterial 3を一組として依存解決・ビルド・表示を確認する。
- 使用フォントの出典とライセンスは`app/src/main/assets/licenses/`を参照する。

設計と実装範囲は[Androidアプリ設計](../../docs/29_Androidアプリ設計.md)を参照する。
リファクタリングには指定の[Material Design 3 UI/UXスキル](https://github.com/skydashnet/material-design-3-ui-skill/tree/a7d28f28251b64740b74dd0046971f23fbe74758)を適用した。
