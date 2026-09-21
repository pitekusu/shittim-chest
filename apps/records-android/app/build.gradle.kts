plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.serialization)
  alias(libs.plugins.metro)
}

android {
  namespace = "dev.pitekusu.shittim.records"
  compileSdk { version = release(37) { minorApiLevel = 1 } }
  buildToolsVersion = "36.0.0"

  defaultConfig {
    applicationId = "dev.pitekusu.shittim.records"
    minSdk = 26
    targetSdk = 37
    versionCode = 1
    versionName = "0.0.1"
    testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
  }

  buildTypes {
    debug {
      applicationIdSuffix = ".dev"
      versionNameSuffix = "-dev"
    }
  }

  compileOptions {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
  }
  buildFeatures { compose = true }
}

kotlin { jvmToolchain(21) }

dependencies {
  // Lint resolves its tool dependencies separately from the Gradle plugin classpath.
  constraints {
    add("androidLintTool", libs.build.commons.lang3)
    add("androidLintTool", libs.build.httpclient)
    add("androidLintTool", libs.build.bouncycastle.bcpkix)
    add("androidLintTool", libs.build.bouncycastle.bcprov)
    add("androidLintTool", libs.build.bouncycastle.bcutil)
  }
  implementation(platform(libs.androidx.compose.bom))
  implementation(libs.androidx.activity.compose)
  implementation(libs.androidx.compose.foundation)
  implementation(libs.androidx.compose.material3)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.circuit.foundation)
  implementation(libs.ktor.client.core)
  implementation(libs.ktor.client.okhttp)
  implementation(libs.kotlinx.serialization.json)
  implementation(libs.kotlinx.coroutines.core)
  debugImplementation(libs.androidx.compose.ui.tooling)
  androidTestImplementation(platform(libs.androidx.compose.bom))
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  androidTestImplementation(libs.androidx.test.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.ktor.client.mock)
}
