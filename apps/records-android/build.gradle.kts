buildscript {
  repositories { mavenCentral() }
  dependencies {
    // AGP's built-in Kotlin and the Compose compiler use the same pinned version.
    classpath(libs.kotlin.gradle.plugin)
  }
}

plugins {
  alias(libs.plugins.android.application) apply false
  alias(libs.plugins.compose.compiler) apply false
}
