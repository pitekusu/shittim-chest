plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.serialization)
  alias(libs.plugins.metro)
}

val appVersionCode = providers.gradleProperty("shittimAndroidVersionCode").orElse("1").get()
  .toIntOrNull()?.takeIf { it > 0 }
  ?: throw GradleException("shittimAndroidVersionCode must be a positive integer")
val appVersionName = providers.gradleProperty("shittimAndroidVersionName").orElse("0.0.1").get()
if (!Regex("[0-9]+\\.[0-9]+\\.[0-9]+").matches(appVersionName)) {
  throw GradleException("shittimAndroidVersionName must use major.minor.patch")
}

// Never keep signing values in Gradle properties, source control, or build output.
val releaseStoreFile = providers.environmentVariable("SHITTIM_ANDROID_UPLOAD_KEYSTORE").orNull
  ?.takeIf { it.isNotBlank() }?.let(::file)
val releaseStorePassword = providers.environmentVariable("SHITTIM_ANDROID_UPLOAD_STORE_PASSWORD").orNull
val releaseKeyAlias = providers.environmentVariable("SHITTIM_ANDROID_UPLOAD_KEY_ALIAS").orNull
val releaseKeyPassword = providers.environmentVariable("SHITTIM_ANDROID_UPLOAD_KEY_PASSWORD").orNull

android {
  namespace = "dev.pitekusu.shittim.records"
  compileSdk { version = release(37) { minorApiLevel = 1 } }
  buildToolsVersion = "36.0.0"

  defaultConfig {
    applicationId = "dev.pitekusu.shittim.records"
    minSdk = 26
    targetSdk = 37
    versionCode = appVersionCode
    versionName = appVersionName
    testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
  }

  signingConfigs {
    create("upload") {
      storeFile = releaseStoreFile
      storePassword = releaseStorePassword
      keyAlias = releaseKeyAlias
      keyPassword = releaseKeyPassword
    }
  }

  buildTypes {
    debug {
      applicationIdSuffix = ".dev"
      versionNameSuffix = "-dev"
    }
    release {
      signingConfig = signingConfigs.getByName("upload")
    }
  }

  compileOptions {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
  }
  buildFeatures { compose = true }
}

// Fail closed before packaging; AGP can otherwise produce an unsigned release artifact.
val verifyReleaseSigning = tasks.register("verifyReleaseSigning") {
  doLast {
    if (releaseStoreFile?.isFile != true ||
      listOf(releaseStorePassword, releaseKeyAlias, releaseKeyPassword).any { it.isNullOrBlank() }) {
      throw GradleException("Android release upload signing inputs are incomplete")
    }
  }
}
tasks.matching { it.name == "preReleaseBuild" }.configureEach {
  dependsOn(verifyReleaseSigning)
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
  implementation(libs.androidx.browser)
  implementation(libs.androidx.lifecycle.viewmodel)
  implementation(libs.androidx.lifecycle.runtime)
  implementation(libs.androidx.compose.foundation)
  implementation(libs.androidx.compose.material3)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.circuit.foundation)
  implementation(libs.ktor.client.core)
  implementation(libs.ktor.client.okhttp)
  implementation(libs.ktor.client.content.negotiation)
  implementation(libs.ktor.serialization.kotlinx.json)
  implementation(libs.kotlinx.serialization.json)
  implementation(libs.kotlinx.coroutines.core)
  implementation(libs.coil.compose)
  implementation(libs.coil.network.okhttp)
  implementation(libs.androidx.paging.runtime)
  implementation(libs.androidx.paging.compose)
  implementation(libs.markdown.core)
  implementation(libs.markdown.material3)
  debugImplementation(libs.androidx.compose.ui.tooling)
  androidTestImplementation(platform(libs.androidx.compose.bom))
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  androidTestImplementation(libs.androidx.test.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.ktor.client.mock)
}
