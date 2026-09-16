# Records Android

C01の最小Composeアプリ。現在は準備画面のみで、認証・通信・記録保存は行わない。
Circuit／Metro、CI、署名済み配布は後続コミットの対象とする。

## 開発環境

- Android Studioではこのディレクトリを開く。
- JDKはTemurinを使用し、バージョンは`.java-version`に合わせる。
- Android SDK Platform 37.0（`platforms;android-37.0`）とBuild Tools 36.0.0を用意する。
- `JAVA_HOME`にJDK、`ANDROID_HOME`にSDKのディレクトリを指定する。
  Android Studioが作る`local.properties`でもSDKを指定できるが、Gitへ追加しない。
- Gradleは同梱Wrapperを使う。プラグイン・ライブラリは`gradle/libs.versions.toml`を正とする。

## 確認

```sh
./gradlew :app:assembleDebug :app:lintDebug
```

APKは`app/build/outputs/apk/debug/app-debug.apk`に出力する。
debug版のapplication IDは`dev.pitekusu.shittim.records.dev`であり、配布版と分離する。
静的な1画面のため、この段階では新しい自動テスト基盤を追加しない。
Previewやビルドの成功は、実機起動・Play配布の確認とは区別する。

設計と実装範囲は[Androidアプリ設計](../../docs/29_Androidアプリ設計.md)を参照する。
