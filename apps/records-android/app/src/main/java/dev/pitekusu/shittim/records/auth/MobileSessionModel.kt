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
import kotlinx.coroutines.cancelAndJoin
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
  private val beginLocalLogout: () -> Unit,
  private val isLocalLogoutPending: () -> Boolean,
  private val completeLocalLogout: () -> Unit,
  private val activateCacheAccount: suspend (String) -> Unit,
  private val clearRecords: suspend () -> Unit,
  private val clock: Clock = Clock.systemUTC(),
) : ViewModel() {
  private val mutableState = MutableStateFlow<SessionState>(SessionState.Checking)
  val state = mutableState.asStateFlow()
  private val mutableCachePermit = MutableStateFlow<CacheAuthorization?>(null)
  val cachePermit = mutableCachePermit.asStateFlow()
  private var token: StoredToken? = null
  private var pendingLogoutToken: StoredToken? = null
  private var operation: Job? = null
  private var expiry: Job? = null
  private val mutableDestination = MutableStateFlow("/")
  val destination = mutableDestination.asStateFlow()
  private var returnTo: String
    get() = mutableDestination.value
    set(value) { mutableDestination.value = value }
  private var offlineAllowed = false
  private var cacheAccessBlocked = false

  val loginDestination: String get() = returnTo

  // Offline readers receive neither credentials nor a persisted profile.
  val offlineCacheAccountId: String? get() {
    val currentState = state.value
    val active = token ?: return null
    val authorization = active.cacheAuthorization ?: return null
    if (mutableCachePermit.value !== authorization) return null
    val readable = (currentState is SessionState.SignedIn &&
      currentState.cacheAccountId == authorization.accountId) ||
      currentState == SessionState.Checking ||
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

  fun onAuthenticationRequired() {
    // A rejected request blocks saved reads immediately, even during an ongoing check.
    // Only a successful server check may issue a new permit; connectivity failure cannot.
    cacheAccessBlocked = true
    mutableCachePermit.value = null
    offlineAllowed = false
    if (state.value is SessionState.SignedIn || state.value == SessionState.Unavailable ||
      state.value == SessionState.Checking) refresh()
  }

  fun onSyncAuthenticationRequired() {
    // WorkData deliberately contains no credential/session identity. The worker
    // invalidates its own stored permit with a token comparison; inspect that result.
    viewModelScope.launch {
      val active = token ?: return@launch
      val stored = try { withContext(Dispatchers.IO) { readToken() } }
      catch (_: TokenStorageException) {
        if (token === active) {
          mutableCachePermit.value = null
          mutableState.value = SessionState.StorageError
        }
        return@launch
      }
      if (token !== active) return@launch
      if (stored?.accessToken == active.accessToken &&
        stored.cacheAuthorization?.accountId == active.cacheAuthorization?.accountId &&
        stored.cacheAuthorization?.permits(clock.instant()) == true) onForeground()
      else onAuthenticationRequired()
    }
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
    if (offlineCacheAccountId == null) return
    val signedIn = state.value as? SessionState.SignedIn
    if (returnTo == "/") return
    returnTo = "/"
    signedIn?.let { mutableState.value = SessionState.SignedIn(it.user, it.cacheAccountId, it.expiresAt, "/") }
  }

  // The token is available only inside the request callback, never in a UI state or saved value.
  // A response that finishes after logout, expiry, or a new session cannot be displayed.
  suspend fun <T> withAuthorizedToken(request: suspend (String) -> T): T? {
    val signedIn = state.value as? SessionState.SignedIn ?: return null
    val active = token ?: return null
    if (!active.expiresAt.isAfter(clock.instant()) || !isCacheAuthorized(signedIn.cacheAccountId)) return null
    val result = try { request(active.accessToken) }
    catch (error: CancellationException) { throw error }
    catch (error: Exception) {
      // An old credential's failure cannot lock a newly authenticated session.
      // The same credential's rejection still matters during a foreground check.
      if (token?.accessToken != active.accessToken) return null
      throw error
    }
    return result.takeIf {
      state.value === signedIn && token === active && active.expiresAt.isAfter(clock.instant()) &&
        isCacheAuthorized(signedIn.cacheAccountId)
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
    val restoredCheck = state.value == SessionState.Checking && offlineCacheAccountId != null
    if (((operation?.isActive == true || state.value == SessionState.Checking) && !restoredCheck) ||
      state.value == SessionState.Browser ||
      state.value is SessionState.SignedOut) return
    val verification = operation.takeIf { restoredCheck }
    verification?.cancel()
    expiry?.cancel()
    val previous = pendingLogoutToken ?: token
    pendingLogoutToken = previous
    token = null
    mutableCachePermit.value = null
    offlineAllowed = false
    mutableState.value = SessionState.SigningOut
    returnTo = "/"
    operation = viewModelScope.launch {
      try {
        finishLogout(previous, verification)
      } catch (error: CancellationException) { throw error }
      catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
      catch (_: RecordCacheException) { mutableState.value = SessionState.StorageError }
    }
  }

  private suspend fun finishLogout(previous: StoredToken?, verification: Job? = null) {
    pendingLogoutToken = previous // A failed local deletion can retry without losing revocation.
    token = null
    mutableCachePermit.value = null
    offlineAllowed = false
    returnTo = "/"
    mutableState.value = SessionState.SigningOut
    withContext(NonCancellable) {
      withContext(Dispatchers.IO) { beginLocalLogout() }
      // Persist deletion intent before waiting; an Activity exit cannot resume the old check.
      verification?.join()
      try { clearRecords() }
      finally { withContext(Dispatchers.IO) { clearToken() } }
      // Do not acknowledge intent if record deletion failed before its own marker was written.
      withContext(Dispatchers.IO) { completeLocalLogout() }
    }
    pendingLogoutToken = null // Claim before the one POST; never replay an uncertain request.
    var notice: SessionNotice? = null
    if (previous != null) {
      try { client.logout(previous.accessToken) }
      catch (error: MobileAuthException) {
        if (error.failure != MobileAuthFailure.AUTHENTICATION_REQUIRED) notice = SessionNotice.LOCAL_LOGOUT
      }
    }
    mutableState.value = SessionState.SignedOut(notice)
  }

  private fun refresh() {
    if (operation?.isActive == true) return
    offlineAllowed = false
    mutableState.value = SessionState.Checking
    operation = viewModelScope.launch {
      try {
        if (withContext(Dispatchers.IO) { isLocalLogoutPending() }) {
          val previous = withContext(Dispatchers.IO) {
            // Credential loss must not block the independent, durable record cleanup request.
            try { readToken() } catch (_: TokenStorageException) { null }
          }
          finishLogout(previous)
          return@launch
        }
        val stored = withContext(Dispatchers.IO) { readToken() }
        token = stored
        if (stored == null) {
          mutableCachePermit.value = null
          mutableState.value = SessionState.SignedOut()
        } else if (!stored.expiresAt.isAfter(clock.instant())) {
          expire()
        } else {
          // Restore only an already-verified, unexpired local permit, never a profile.
          // Repository reads still require the matching existing cache owner on disk.
          mutableCachePermit.value = stored.cacheAuthorization?.takeIf {
            !cacheAccessBlocked && it.permits(clock.instant())
          }
          scheduleExpiry(minOf(stored.expiresAt, stored.cacheAuthorization?.expiresAt ?: stored.expiresAt))
          val response = client.session(stored.accessToken)
          // Neither a stale response nor a longer server value can extend the saved deadline.
          val verifiedAt = clock.instant().truncatedTo(ChronoUnit.SECONDS)
          val deadline = minOf(stored.expiresAt, response.expiresAt, verifiedAt.plus(Duration.ofDays(90)))
          if (!deadline.isAfter(clock.instant())) expire()
          else {
            // Switch/erase before issuing a new permit, even when no record is fetched afterward.
            if (mutableCachePermit.value?.accountId != response.cacheAccountId) mutableCachePermit.value = null
            activateCacheAccount(response.cacheAccountId)
            val authorized = StoredToken(stored.accessToken, deadline,
              CacheAuthorization(response.cacheAccountId, verifiedAt, deadline))
            withContext(Dispatchers.IO) { saveToken(authorized) }
            token = authorized
            if (!deadline.isAfter(clock.instant())) expire()
            else {
              cacheAccessBlocked = false
              mutableCachePermit.value = authorized.cacheAuthorization
              mutableState.value = SessionState.SignedIn(response.user, response.cacheAccountId, deadline, returnTo)
              scheduleExpiry(deadline)
            }
          }
        }
      } catch (error: CancellationException) { throw error }
      catch (_: TokenStorageException) {
        mutableCachePermit.value = null
        mutableState.value = SessionState.StorageError
      }
      catch (_: RecordCacheException) {
        mutableCachePermit.value = null
        mutableState.value = SessionState.StorageError
      }
      catch (error: MobileAuthException) {
        if (error.failure in setOf(MobileAuthFailure.AUTHENTICATION_REQUIRED, MobileAuthFailure.FORBIDDEN)) {
          try { expire() }
          catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
        } else {
          // Only connectivity/service failures allow the already-verified offline permit.
          offlineAllowed = !cacheAccessBlocked &&
            error.failure in setOf(MobileAuthFailure.NETWORK, MobileAuthFailure.UNAVAILABLE)
          if (!offlineAllowed) mutableCachePermit.value = null
          mutableState.value = SessionState.Unavailable
          token?.let { stored ->
            scheduleExpiry(minOf(stored.expiresAt, stored.cacheAuthorization?.expiresAt ?: stored.expiresAt))
          }
        }
      }
    }
  }

  private fun scheduleExpiry(deadline: Instant) {
    expiry?.cancel()
    expiry = viewModelScope.launch {
      delay(Duration.between(clock.instant(), deadline).toMillis().coerceAtLeast(1))
      // The monotonic delay cannot be prolonged by moving the wall clock backward in this process.
      mutableCachePermit.value = null
      mutableState.value = SessionState.Checking
      // Join any token-storage write before deletion; a late check cannot restore access.
      operation?.cancelAndJoin()
      try { expire() }
      catch (_: TokenStorageException) { mutableState.value = SessionState.StorageError }
    }
  }

  private suspend fun expire() {
    offlineAllowed = false
    token = null
    mutableCachePermit.value = null
    withContext(Dispatchers.IO) { clearToken() }
    // Preserve record keys/ciphertext. Same-account reauthentication can unlock them again.
    // Keep a validated record link across expiry so the next login can return to it.
    // Explicit logout still clears the destination when switching accounts.
    mutableState.value = SessionState.SignedOut(SessionNotice.EXPIRED)
  }

  override fun onCleared() {
    expiry?.cancel()
    token = null
    mutableCachePermit.value = null
    pendingLogoutToken = null
    client.close()
  }
}
