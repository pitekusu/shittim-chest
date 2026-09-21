package dev.pitekusu.shittim.records.auth

import androidx.annotation.MainThread
import androidx.browser.auth.AuthTabIntent
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

internal sealed interface MobileLoginStep {
  data object Idle : MobileLoginStep
  data object Starting : MobileLoginStep
  class OpenBrowser(val url: String) : MobileLoginStep
  data object InBrowser : MobileLoginStep
  data object Exchanging : MobileLoginStep
  class Finished(val status: MobileLoginStatus, val returnTo: String? = null) : MobileLoginStep
}

/** Retained across rotation, never saved to a Bundle. C16 owns the visible session UI. */
@MainThread
internal class MobileLoginModel(
  private val client: MobileAuthClient,
  private val saveToken: (StoredToken) -> Unit,
) : ViewModel() {
  private val flow = MobileLoginFlow(client)
  private val mutableStep = MutableStateFlow<MobileLoginStep>(MobileLoginStep.Idle)
  val step = mutableStep.asStateFlow()

  fun begin(returnTo: String) {
    if (step.value != MobileLoginStep.Idle) return
    mutableStep.value = MobileLoginStep.Starting
    runOperation { mutableStep.value = MobileLoginStep.OpenBrowser(flow.begin(returnTo)) }
  }

  fun browserLaunched() {
    if (step.value is MobileLoginStep.OpenBrowser) mutableStep.value = MobileLoginStep.InBrowser
  }

  fun browserResult(result: AuthTabIntent.AuthResult) {
    when (result.resultCode) {
      AuthTabIntent.RESULT_OK -> result.resultUri?.toString()?.let(::complete)
        ?: finish(MobileLoginStatus.REJECTED)
      AuthTabIntent.RESULT_CANCELED -> finish(MobileLoginStatus.CANCELLED)
      // Never bypass failed HTTPS ownership verification by retrying in an ordinary browser.
      else -> finish(MobileLoginStatus.REJECTED)
    }
  }

  fun complete(url: String) {
    // In particular, a cold process has no verifier and must not create a new transaction.
    if (step.value == MobileLoginStep.Idle) {
      finish(MobileLoginStatus.REJECTED)
      return
    }
    if (step.value != MobileLoginStep.InBrowser) return
    mutableStep.value = MobileLoginStep.Exchanging
    runOperation {
      val response = flow.complete(url)
      withContext(Dispatchers.IO) { saveToken(StoredToken(response.accessToken, response.expiresAt)) }
      mutableStep.value = MobileLoginStep.Finished(MobileLoginStatus.SIGNED_IN, response.returnTo)
    }
  }

  fun finish(status: MobileLoginStatus) {
    if (step.value == MobileLoginStep.Exchanging || step.value is MobileLoginStep.Finished) return
    flow.cancel()
    mutableStep.value = MobileLoginStep.Finished(status)
  }

  private fun runOperation(operation: suspend () -> Unit) {
    viewModelScope.launch {
      try {
        operation()
      } catch (error: CancellationException) {
        throw error
      } catch (error: Exception) {
        flow.cancel()
        val status = when (error) {
          is MobileLoginException -> error.status
          is TokenStorageException -> MobileLoginStatus.STORAGE_UNAVAILABLE
          is MobileAuthException -> when (error.failure) {
            MobileAuthFailure.NETWORK -> MobileLoginStatus.NETWORK
            MobileAuthFailure.THROTTLED, MobileAuthFailure.UNAVAILABLE -> MobileLoginStatus.UNAVAILABLE
            else -> MobileLoginStatus.REJECTED
          }
          else -> MobileLoginStatus.REJECTED
        }
        mutableStep.value = MobileLoginStep.Finished(status)
      }
    }
  }

  override fun onCleared() {
    flow.cancel()
    client.close()
  }
}
