package dev.pitekusu.shittim.records.auth

import androidx.annotation.MainThread
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import java.time.Clock
import java.time.Duration
import java.time.Instant
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

internal enum class SessionNotice { EXPIRED, CANCELLED, LOGIN_FAILED, BROWSER_UNAVAILABLE, LOCAL_LOGOUT }

// No credentials or saved UI state. Leaving SignedIn drops the previous user's profile.
internal sealed interface SessionState {
  data object Checking : SessionState
  class SignedOut(val notice: SessionNotice? = null) : SessionState
  data object Browser : SessionState
  class SignedIn(val user: MobileSessionUser, val expiresAt: Instant, val returnTo: String) : SessionState
  data object SigningOut : SessionState
  data object Unavailable : SessionState
  data object StorageError : SessionState
}

/** Activity-retained authentication lifetime; Circuit owns the screen and its presentation. */
@MainThread
internal class MobileSessionModel(
  private val client: MobileAuthClient,
  private val readToken: () -> StoredToken?,
  private val clearToken: () -> Unit,
  private val clock: Clock = Clock.systemUTC(),
) : ViewModel() {
  private val mutableState = MutableStateFlow<SessionState>(SessionState.Checking)
  val state = mutableState.asStateFlow()
  private var token: StoredToken? = null
  private var operation: Job? = null
  private var expiry: Job? = null
  private var returnTo = "/"

  init { refresh() }

  fun onForeground() {
    if (state.value is SessionState.SignedIn || state.value == SessionState.Unavailable) refresh()
  }

  fun retry() {
    if (state.value == SessionState.Unavailable) refresh()
  }

  // Claim synchronously, before launching the Activity, to ignore double taps.
  fun beginLogin(): Boolean {
    if (state.value !is SessionState.SignedOut || operation?.isActive == true) return false
    mutableState.value = SessionState.Browser
    return true
  }

  fun loginResult(result: MobileLoginStep.Finished) {
    if (state.value != SessionState.Browser) return
    if (result.status == MobileLoginStatus.SIGNED_IN) {
      returnTo = result.returnTo?.takeIf(::isMobileReturnTo) ?: "/"
      refresh() // C15 has saved the token. Re-read and validate it, not the Activity result.
    } else {
      mutableState.value = when (result.status) {
        MobileLoginStatus.STORAGE_UNAVAILABLE -> SessionState.StorageError
        MobileLoginStatus.CANCELLED -> SessionState.SignedOut(SessionNotice.CANCELLED)
        MobileLoginStatus.EXPIRED -> SessionState.SignedOut(SessionNotice.EXPIRED)
        MobileLoginStatus.BROWSER_UNAVAILABLE -> SessionState.SignedOut(SessionNotice.BROWSER_UNAVAILABLE)
        else -> SessionState.SignedOut(SessionNotice.LOGIN_FAILED)
      }
    }
  }

  fun logout() {
    if (operation?.isActive == true || state.value == SessionState.Checking || state.value == SessionState.Browser ||
      state.value is SessionState.SignedOut) return
    expiry?.cancel()
    val previous = token
    mutableState.value = SessionState.SigningOut
    returnTo = "/"
    operation = viewModelScope.launch {
      try {
        // Remove local access even if the network fails; never call an uncertain POST twice.
        withContext(Dispatchers.IO) { clearToken() }
        token = null
        var notice: SessionNotice? = null
        if (previous != null) {
          try { client.logout(previous.accessToken) }
          catch (error: MobileAuthException) {
            if (error.failure != MobileAuthFailure.AUTHENTICATION_REQUIRED) notice = SessionNotice.LOCAL_LOGOUT
          }
        }
        mutableState.value = SessionState.SignedOut(notice)
      } catch (error: CancellationException) { throw error }
      catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
    }
  }

  private fun refresh() {
    if (operation?.isActive == true) return
    expiry?.cancel()
    mutableState.value = SessionState.Checking
    operation = viewModelScope.launch {
      try {
        val stored = withContext(Dispatchers.IO) { readToken() }
        token = stored
        if (stored == null) {
          mutableState.value = SessionState.SignedOut()
        } else if (!stored.expiresAt.isAfter(clock.instant())) {
          expire()
        } else {
          val response = client.session(stored.accessToken)
          // Neither a stale response nor a longer server value can extend the saved deadline.
          val deadline = minOf(stored.expiresAt, response.expiresAt)
          if (!deadline.isAfter(clock.instant())) expire()
          else {
            mutableState.value = SessionState.SignedIn(response.user, deadline, returnTo)
            expiry = viewModelScope.launch {
              delay(Duration.between(clock.instant(), deadline).toMillis().coerceAtLeast(1))
              // Delay uses monotonic time, so moving the wall clock back cannot extend this session.
              mutableState.value = SessionState.Checking
              try { expire() }
              catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
            }
          }
        }
      } catch (error: CancellationException) { throw error }
      catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
      catch (error: MobileAuthException) {
        if (error.failure == MobileAuthFailure.AUTHENTICATION_REQUIRED) {
          try { expire() }
          catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
        } else mutableState.value = SessionState.Unavailable
      }
    }
  }

  private suspend fun expire() {
    withContext(Dispatchers.IO) { clearToken() }
    token = null
    returnTo = "/"
    mutableState.value = SessionState.SignedOut(SessionNotice.EXPIRED)
  }

  override fun onCleared() {
    expiry?.cancel()
    token = null
    client.close()
  }
}
