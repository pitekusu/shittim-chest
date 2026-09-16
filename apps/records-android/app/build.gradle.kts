plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
}

android {
  namespace = "dev.pitekusu.shittim.records"
  compileSdk = 37
  buildToolsVersion = "36.0.0"

  defaultConfig {
    applicationId = "dev.pitekusu.shittim.records"
    minSdk = 26
    targetSdk = 37
    versionCode = 1
    versionName = "0.0.1"
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
  implementation(platform(libs.androidx.compose.bom))
  implementation(libs.androidx.activity.compose)
  implementation(libs.androidx.compose.foundation)
  implementation(libs.androidx.compose.material3)
  implementation(libs.androidx.compose.ui.tooling.preview)
  debugImplementation(libs.androidx.compose.ui.tooling)
}
