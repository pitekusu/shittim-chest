package dev.pitekusu.shittim.records.auth

import android.content.Intent
import android.net.Uri
import android.os.Looper
import androidx.browser.auth.AuthTabIntent
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MobileLoginModelTest {
  @Test
  fun exchangeSavesOnceOffMainAndDoesNotExposeTokenInTheActivityResult() = runBlocking {
    withContext(Dispatchers.Main) {
      MobileLoginFixture().use { fixture ->
        var saves = 0
        val model = MobileLoginModel(fixture.client) { token ->
          assertTrue(Looper.myLooper() != Looper.getMainLooper())
          assertEquals(fixture.token, token.accessToken)
          saves++
        }
        val owner = ViewModelStore().apply { put("login", model) }
        try {
          model.begin("/")
          model.begin("/")
          withTimeout(5_000) { model.step.first { it is MobileLoginStep.OpenBrowser } }
          model.browserLaunched()
          val callback = fixture.callback()
          model.browserResult(browserResult(AuthTabIntent.RESULT_OK, callback))
          model.complete(callback)
          // A Custom Tab fallback can deliver the same redirect, then close with RESULT_CANCELED.
          model.browserResult(browserResult(AuthTabIntent.RESULT_CANCELED))
          val result = finished(model)
          assertEquals(MobileLoginStatus.SIGNED_IN, result.status)
          assertEquals("/", result.returnTo)
          assertFalse(result.toString().contains(fixture.token))
          assertEquals(1, fixture.starts)
          assertEquals(1, fixture.exchanges)
          assertEquals(1, saves)
        } finally { owner.clear() }
      }
    }
  }

  @Test
  fun browserCancellationAndLostProcessNeverExchangeOrSave() = runBlocking {
    withContext(Dispatchers.Main) {
      MobileLoginFixture().use { fixture ->
        val model = MobileLoginModel(fixture.client) { error("must_not_save") }
        val owner = ViewModelStore().apply { put("login", model) }
        try {
          model.begin("/")
          withTimeout(5_000) { model.step.first { it is MobileLoginStep.OpenBrowser } }
          model.browserLaunched()
          val callback = fixture.callback()
          model.browserResult(browserResult(AuthTabIntent.RESULT_CANCELED))
          model.complete(callback)
          assertEquals(MobileLoginStatus.CANCELLED, finished(model).status)
          val cold = MobileLoginModel(fixture.client) { error("must_not_save") }
          owner.put("cold", cold)
          cold.browserResult(browserResult(AuthTabIntent.RESULT_OK, callback))
          assertEquals(MobileLoginStatus.REJECTED, finished(cold).status)
          assertEquals(1, fixture.starts)
          assertEquals(0, fixture.exchanges)
        } finally { owner.clear() }
      }
    }
  }

  @Test
  fun failedHttpsVerificationAndMissingResultNeverExchangeOrFallback() = runBlocking {
    withContext(Dispatchers.Main) {
      for (code in listOf(AuthTabIntent.RESULT_VERIFICATION_FAILED,
        AuthTabIntent.RESULT_VERIFICATION_TIMED_OUT, AuthTabIntent.RESULT_OK, 12345)) {
        MobileLoginFixture().use { fixture ->
          val model = MobileLoginModel(fixture.client) { error("must_not_save") }
          val owner = ViewModelStore().apply { put("login", model) }
          try {
            model.begin("/")
            withTimeout(5_000) { model.step.first { it is MobileLoginStep.OpenBrowser } }
            model.browserLaunched()
            model.browserResult(browserResult(code))
            model.complete(fixture.callback())
            assertEquals(MobileLoginStatus.REJECTED, finished(model).status)
            assertEquals(1, fixture.starts)
            assertEquals(0, fixture.exchanges)
          } finally { owner.clear() }
        }
      }
    }
  }

  @Test
  fun tokenStorageFailureIsNotReportedAsSuccessfulLoginOrAutomaticallyRetried() = runBlocking {
    withContext(Dispatchers.Main) {
      MobileLoginFixture().use { fixture ->
        var saves = 0
        val model = MobileLoginModel(fixture.client) { saves++; throw TokenStorageException() }
        val owner = ViewModelStore().apply { put("login", model) }
        try {
          model.begin("/")
          withTimeout(5_000) { model.step.first { it is MobileLoginStep.OpenBrowser } }
          model.browserLaunched()
          model.complete(fixture.callback())
          val result = finished(model)
          assertEquals(MobileLoginStatus.STORAGE_UNAVAILABLE, result.status)
          assertNull(result.returnTo)
          model.complete(fixture.callback())
          assertEquals(1, fixture.exchanges)
          assertEquals(1, saves)
        } finally { owner.clear() }
      }
    }
  }

  private suspend fun finished(model: MobileLoginModel): MobileLoginStep.Finished = withTimeout(5_000) {
    model.step.first { it is MobileLoginStep.Finished } as MobileLoginStep.Finished
  }

  private fun browserResult(code: Int, url: String? = null): AuthTabIntent.AuthResult =
    AuthTabIntent.AuthenticateUserResultContract().parseResult(code,
      url?.let { Intent().setData(Uri.parse(it)) })
}
