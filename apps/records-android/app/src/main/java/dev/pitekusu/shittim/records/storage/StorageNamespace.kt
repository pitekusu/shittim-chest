package dev.pitekusu.shittim.records.storage

/** Keep deployed production names intact; review data owns different files and Keystore aliases. */
internal enum class StorageNamespace {
  PRODUCTION,
  DEMO;

  fun name(productionName: String): String = when (this) {
    PRODUCTION -> productionName
    DEMO -> "review-demo-$productionName"
  }
}
