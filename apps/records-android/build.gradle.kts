buildscript {
  repositories { mavenCentral() }
  dependencies {
    // AGP's built-in Kotlin and the Compose compiler use the same pinned version.
    classpath(libs.kotlin.gradle.plugin)
    // Explicit constraints let Dependabot update vulnerable AGP transitives.
    constraints {
      classpath(libs.build.commons.lang3) { because("GHSA-j288-q9x7-2f5v") }
      classpath(libs.build.httpclient) { because("GHSA-7r82-7xv7-xcpj") }
      classpath(libs.build.jose4j) { because("GHSA-3677-xxcr-wjqv") }
      classpath(libs.build.bouncycastle.bcpkix) { because("GHSA-wg6q-6289-32hp") }
      classpath(libs.build.bouncycastle.bcprov) {
        because("GHSA-9pwp-9qqc-pr26 and GHSA-qp49-qgx5-5m26")
      }
      classpath(libs.build.bouncycastle.bcutil) { because("Align Bouncy Castle modules") }
      classpath(libs.build.jdom2) { because("GHSA-2363-cqg2-863c") }
    }
  }
}

plugins {
  alias(libs.plugins.android.application) apply false
  alias(libs.plugins.compose.compiler) apply false
  alias(libs.plugins.serialization) apply false
  alias(libs.plugins.metro) apply false
  alias(libs.plugins.ksp) apply false
  alias(libs.plugins.room3) apply false
}
