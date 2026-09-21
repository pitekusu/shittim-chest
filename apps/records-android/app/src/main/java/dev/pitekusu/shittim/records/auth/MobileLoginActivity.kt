package dev.pitekusu.shittim.records.auth

import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.viewModels
import androidx.browser.auth.AuthTabIntent
import androidx.browser.customtabs.CustomTabsIntent
import androidx.core.net.toUri
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.lifecycle.withResumed
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import kotlinx.coroutines.launch

/** Private browser controller; only the redirect receiver is exported. No WebView or saved grant. */
class MobileLoginActivity : ComponentActivity() {
  private val browser = AuthTabIntent.registerActivityResultLauncher(this) { result ->
    if (result.resultCode == AuthTabIntent.RESULT_CANCELED) {
      // A fallback redirect can also close the Custom Tab. Consume onNewIntent before cancellation.
      lifecycleScope.launch { lifecycle.withResumed { login.browserResult(result) } }
    } else login.browserResult(result)
  }

  private val login by viewModels<MobileLoginModel> {
    viewModelFactory { initializer {
      MobileLoginModel(MobileAuthClient(), KeystoreTokenStore(applicationContext)::save)
    } }
  }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    val callback = intent.dataString
    val returnTo = intent.getStringExtra(LOGIN_RETURN_TO)
    setIntent(Intent(this, MobileLoginActivity::class.java)) // Do not retain the callback in Activity state.
    when {
      callback != null -> login.complete(callback)
      savedInstanceState == null && isMobileReturnTo(returnTo) -> login.begin(requireNotNull(returnTo))
      login.step.value == MobileLoginStep.Idle -> login.finish(MobileLoginStatus.REJECTED)
    }
    (login.step.value as? MobileLoginStep.Finished)?.let {
      finishWithResult(it)
      return
    }
    lifecycleScope.launch {
      repeatOnLifecycle(Lifecycle.State.RESUMED) {
        login.step.collect { step ->
          when (step) {
            is MobileLoginStep.OpenBrowser -> {
              login.browserLaunched()
              try {
                val authTab = AuthTabIntent.Builder().build()
                authTab.intent.putExtra(CustomTabsIntent.EXTRA_SHARE_STATE, CustomTabsIntent.SHARE_STATE_OFF)
                val callbackUri = MOBILE_CALLBACK_URL.toUri()
                authTab.launch(browser, step.url.toUri(), requireNotNull(callbackUri.host),
                  requireNotNull(callbackUri.path))
              } catch (_: ActivityNotFoundException) {
                login.finish(MobileLoginStatus.BROWSER_UNAVAILABLE)
              } catch (_: SecurityException) {
                login.finish(MobileLoginStatus.BROWSER_UNAVAILABLE)
              }
            }
            is MobileLoginStep.Finished -> finishWithResult(step)
            else -> Unit
          }
        }
      }
    }
  }

  override fun onNewIntent(intent: Intent) {
    super.onNewIntent(intent)
    intent.dataString?.let(login::complete)
    setIntent(Intent(this, MobileLoginActivity::class.java))
  }

  private fun finishWithResult(result: MobileLoginStep.Finished) {
    setResult(Activity.RESULT_OK, Intent().putExtra(LOGIN_STATUS, result.status.name)
      .putExtra(LOGIN_RETURN_TO, result.returnTo))
    finish()
  }
}

/** Accept only the fixed callback shape; transaction/state/expiry are checked by the live flow. */
class MobileLoginRedirectActivity : Activity() {
  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    val action = intent.action
    val url = intent.dataString
    setIntent(Intent())
    if (action != Intent.ACTION_VIEW) {
      finish()
      return
    }
    try {
      parseMobileCallback(requireNotNull(url))
      startActivity(Intent(this, MobileLoginActivity::class.java).setData(url.toUri())
        .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP))
    } catch (_: IllegalArgumentException) {
      // Invalid or missing data never starts native authentication.
    } catch (_: MobileLoginException) {
      // Neither URL nor query values enter logs or an exception chain.
    } finally {
      finish()
    }
  }
}
