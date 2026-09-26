package dev.pitekusu.shittim.records.auth

import androidx.annotation.MainThread
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import dev.pitekusu.shittim.records.storage.RecordCacheException
import java.time.Clock
import java.time.Duration
import java.time.Instant
import java.time.temporal.ChronoUnit
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
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
  class SignedIn(val user: MobileSessionUser, val cacheAccountId: String,
    val expiresAt: Instant, val returnTo: String) : SessionState
  data object SigningOut : SessionState
  data object Unavailable : SessionState
  data object StorageError : SessionState
}

/** Activity-retained authentication lifetime; Circuit owns the screen and its presentation. */
@MainThread
internal class MobileSessionModel(
  private val client: MobileAuthClient,
  private val readToken: () -> StoredToken?,
  private val saveToken: (StoredToken) -> Unit,
  private val clearToken: () -> Unit,
  private val activateCacheAccount: suspend (String) -> Unit,
  private val clearRecords: suspend () -> Unit,
  private val clock: Clock = Clock.systemUTC(),
) : ViewModel() {
  private val mutableState = MutableStateFlow<SessionState>(SessionState.Checking)
  val state = mutableState.asStateFlow()
  private var token: StoredToken? = null
  private var operation: Job? = null
  private var expiry: Job? = null
  private var returnTo = "/"
  private var offlineAllowed = false

  val loginDestination: String get() = returnTo

  // C31/C32 can read this gate without credentials or a saved profile. The UI remains online-only.
  val offlineCacheAccountId: String? get() {
    val currentState = state.value
    val active = token ?: return null
    val authorization = active.cacheAuthorization ?: return null
    val readable = (currentState is SessionState.SignedIn &&
      currentState.cacheAccountId == authorization.accountId) ||
      (currentState == SessionState.Unavailable && offlineAllowed)
    return authorization.accountId.takeIf {
      readable && authorization.permits(clock.instant()) && state.value === currentState && token === active
    }
  }

  fun isCacheAuthorized(accountId: String): Boolean = offlineCacheAccountId == accountId

  init { refresh() }

  fun onForeground() {
    if (state.value is SessionState.SignedIn || state.value == SessionState.Unavailable) refresh()
  }

  fun retry() {
    if (state.value == SessionState.Unavailable) refresh()
  }

  fun openDestination(destination: String) {
    if (!destination.startsWith("/records/") || !isMobileReturnTo(destination) ||
      state.value == SessionState.Browser || state.value == SessionState.SigningOut) return
    if (returnTo == destination) return
    returnTo = destination
    (state.value as? SessionState.SignedIn)?.let {
      mutableState.value = SessionState.SignedIn(it.user, it.cacheAccountId, it.expiresAt, destination)
    }
  }

  fun closeDestination() {
    val signedIn = state.value as? SessionState.SignedIn ?: return
    if (returnTo == "/") return
    returnTo = "/"
    mutableState.value = SessionState.SignedIn(signedIn.user, signedIn.cacheAccountId, signedIn.expiresAt, "/")
  }

  // The token is available only inside the request callback, never in a UI state or saved value.
  // A response that finishes after logout, expiry, or a new session cannot be displayed.
  suspend fun <T> withAuthorizedToken(request: suspend (String) -> T): T? {
    val signedIn = state.value as? SessionState.SignedIn ?: return null
    val active = token ?: return null
    if (!active.expiresAt.isAfter(clock.instant())) return null
    val result = request(active.accessToken)
    return result.takeIf {
      state.value === signedIn && token === active && active.expiresAt.isAfter(clock.instant())
    }
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
    token = null
    offlineAllowed = false
    mutableState.value = SessionState.SigningOut
    returnTo = "/"
    operation = viewModelScope.launch {
      try {
        // Remove local access even if the network fails; never call an uncertain POST twice.
        // Finish local deletion even if the Activity/ViewModel disappears during logout.
        withContext(NonCancellable) {
          try { clearRecords() }
          finally { withContext(Dispatchers.IO) { clearToken() } }
        }
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
      catch (_: RecordCacheException) { mutableState.value = SessionState.StorageError }
    }
  }

  private fun refresh() {
    if (operation?.isActive == true) return
    expiry?.cancel()
    offlineAllowed = false
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
          val verifiedAt = clock.instant().truncatedTo(ChronoUnit.SECONDS)
          val deadline = minOf(stored.expiresAt, response.expiresAt, verifiedAt.plus(Duration.ofDays(90)))
          if (!deadline.isAfter(clock.instant())) expire()
          else {
            // Switch/erase before issuing a new permit, even when no record is fetched afterward.
            activateCacheAccount(response.cacheAccountId)
            val authorized = StoredToken(stored.accessToken, deadline,
              CacheAuthorization(response.cacheAccountId, verifiedAt, deadline))
            withContext(Dispatchers.IO) { saveToken(authorized) }
            token = authorized
            if (!deadline.isAfter(clock.instant())) expire()
            else {
              mutableState.value = SessionState.SignedIn(response.user, response.cacheAccountId, deadline, returnTo)
              scheduleExpiry(deadline)
            }
          }
        }
      } catch (error: CancellationException) { throw error }
      catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
      catch (_: RecordCacheException) { mutableState.value = SessionState.StorageError }
      catch (error: MobileAuthException) {
        if (error.failure in setOf(MobileAuthFailure.AUTHENTICATION_REQUIRED, MobileAuthFailure.FORBIDDEN)) {
          try { expire() }
          catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
        } else {
          // Only connectivity/service failures allow the already-verified offline permit.
          offlineAllowed = error.failure in setOf(MobileAuthFailure.NETWORK, MobileAuthFailure.UNAVAILABLE)
          mutableState.value = SessionState.Unavailable
          token?.let { stored ->
            scheduleExpiry(minOf(stored.expiresAt, stored.cacheAuthorization?.expiresAt ?: stored.expiresAt))
          }
        }
      }
    }
  }

  private fun scheduleExpiry(deadline: Instant) {
    expiry = viewModelScope.launch {
      delay(Duration.between(clock.instant(), deadline).toMillis().coerceAtLeast(1))
      // The monotonic delay cannot be prolonged by moving the wall clock backward in this process.
      mutableState.value = SessionState.Checking
      try { expire() }
      catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
    }
  }

  private suspend fun expire() {
    offlineAllowed = false
    token = null
    withContext(Dispatchers.IO) { clearToken() }
    // Preserve record keys/ciphertext. Same-account reauthentication can unlock them again.
    // Keep a validated record link across expiry so the next login can return to it.
    // Explicit logout still clears the destination when switching accounts.
    mutableState.value = SessionState.SignedOut(SessionNotice.EXPIRED)
  }

  override fun onCleared() {
    expiry?.cancel()
    token = null
    client.close()
  }
}
