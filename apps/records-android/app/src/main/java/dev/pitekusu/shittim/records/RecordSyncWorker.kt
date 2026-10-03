package dev.pitekusu.shittim.records

import android.content.Context
import android.os.SystemClock
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.MobileAuthException
import dev.pitekusu.shittim.records.auth.MobileAuthFailure
import dev.pitekusu.shittim.records.auth.StoredToken
import dev.pitekusu.shittim.records.auth.MobileSessionResponse
import java.time.Clock
import java.time.Instant
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

/** Credentials are read at execution time, never serialized into WorkManager's database. */
internal class RecordSyncWorker(context: Context, parameters: WorkerParameters,
  private val store: KeystoreTokenStore,
  private val verifySession: suspend (String) -> MobileSessionResponse,
  private val openRepository: (RecordSyncLease) -> RecordsRepository,
) : CoroutineWorker(context, parameters) {
  constructor(context: Context, parameters: WorkerParameters) : this(context, parameters,
    KeystoreTokenStore(context),
    { token -> MobileAuthClient().use { it.session(token) } },
    { lease -> RecordsRepository.open(context, lease::permits, activateOwner = false) })

  override suspend fun doWork(): Result = withContext(Dispatchers.IO) { coroutineScope {
    var observedToken: StoredToken? = null
    val notifications = RecordSyncNotifications(this, RecordSyncScheduler::cacheChanged)
    try {
      val stored = if (store.isLogoutPending()) null else store.read()
      observedToken = stored
      val permit = stored?.cacheAuthorization
      if (stored == null || permit == null || !permit.permits(Instant.now())) {
        return@coroutineScope stopped(RecordReadFailure.AUTH_REQUIRED)
      }
      val verified = verifySession(stored.accessToken)
      if (verified.cacheAccountId != permit.accountId || !verified.expiresAt.isAfter(Instant.now())) {
        RecordNotifications.revokeMatchingSession(applicationContext, stored.accessToken)
        store.invalidateCacheAuthorization(stored.accessToken)
        return@coroutineScope stopped(RecordReadFailure.AUTH_REQUIRED)
      }
      val lease = RecordSyncLease(store, stored, minOf(permit.expiresAt, verified.expiresAt))
      // A worker must not reactivate an owner after logout/account switching erased it.
      openRepository(lease).use { records ->
        val finished = withTimeoutOrNull(8 * 60_000L) {
          records.synchronize(stored.accessToken, permit.accountId) {
            if (notifications.saved()) setProgress(workDataOf(
              "updatedAt" to System.currentTimeMillis(),
              "cacheGeneration" to RecordSyncScheduler.cacheChanges.value))
          }
          true
        }
        if (finished != true) return@coroutineScope retryOrStop(RecordReadFailure.UNAVAILABLE)
      }
      Result.success(workDataOf("finishedAt" to System.currentTimeMillis()))
    } catch (error: CancellationException) { throw error }
    catch (error: MobileAuthException) {
      if (error.failure in setOf(MobileAuthFailure.AUTHENTICATION_REQUIRED, MobileAuthFailure.FORBIDDEN)) {
        observedToken?.let { RecordNotifications.revokeMatchingSession(applicationContext, it.accessToken) }
        try { observedToken?.let { store.invalidateCacheAuthorization(it.accessToken) } }
        catch (_: Exception) { return@coroutineScope stopped(RecordReadFailure.STORAGE_UNAVAILABLE) }
        stopped(RecordReadFailure.AUTH_REQUIRED)
      } else retryOrStop(if (error.failure in setOf(MobileAuthFailure.NETWORK,
        MobileAuthFailure.UNAVAILABLE, MobileAuthFailure.THROTTLED)) RecordReadFailure.UNAVAILABLE
        else RecordReadFailure.INVALID_RESPONSE)
    } catch (error: RecordReadException) {
      if (error.failure == RecordReadFailure.AUTH_REQUIRED) {
        observedToken?.let { RecordNotifications.revokeMatchingSession(applicationContext, it.accessToken) }
        try { observedToken?.let { store.invalidateCacheAuthorization(it.accessToken) } }
        catch (_: Exception) { return@coroutineScope stopped(RecordReadFailure.STORAGE_UNAVAILABLE) }
      }
      retryOrStop(error.failure)
    }
    catch (_: Exception) { stopped(RecordReadFailure.STORAGE_UNAVAILABLE) }
    finally {
      // Includes partial success before failure/cancellation and the last image/deletion.
      // StateFlow emission does not suspend, so cancellation cannot discard a committed save.
      notifications.close()
    }
  } }

  private fun retryOrStop(reason: RecordReadFailure): Result =
    if (reason == RecordReadFailure.UNAVAILABLE && runAttemptCount < 3) Result.retry() else stopped(reason)

  private fun stopped(reason: RecordReadFailure): Result = Result.failure(workDataOf(
    "failure" to reason.name, "finishedAt" to System.currentTimeMillis()))
}

/** Saves stay immediate; a single timer bounds notification bursts without waiting for more IO. */
internal class RecordSyncNotifications(private val scope: CoroutineScope, private val notify: () -> Unit,
  private val now: () -> Long = SystemClock::elapsedRealtime,
  private val pause: suspend (Long) -> Unit = { delay(it) }) {
  private val lock = Any()
  private var lastNotification: Long? = null
  private var pending = false
  private var timer: Job? = null
  private var timerGeneration = 0L
  private var closed = false

  fun saved(): Boolean = synchronized(lock) {
    if (closed) return@synchronized false
    pending = true
    val previous = lastNotification
    val elapsed = previous?.let { now() - it }
    if (elapsed == null || elapsed >= 500) {
      cancelTimer()
      emit()
    } else {
      if (timer == null) {
        val generation = ++timerGeneration
        val delayed = scope.launch(start = CoroutineStart.LAZY) {
          pause(500 - elapsed)
          synchronized(lock) {
            if (!closed && generation == timerGeneration) {
              timer = null
              emit()
            }
          }
        }
        timer = delayed
        delayed.start()
      }
      false
    }
  }

  fun flush(): Boolean = synchronized(lock) { cancelTimer(); emit() }

  fun close(): Boolean = synchronized(lock) {
    closed = true
    cancelTimer()
    emit()
  }

  private fun cancelTimer() {
    timerGeneration++
    timer?.cancel()
    timer = null
  }

  // These scalar operations can run on the worker and its timer, so keep them in one short lock.
  // The generation is emitted synchronously before optional WorkManager progress can fail.
  private fun emit(): Boolean {
    if (!pending) return false
    pending = false
    lastNotification = now()
    notify()
    return true
  }
}

/** Rechecks persisted deletion intent, token identity and absolute deadline at every cache boundary. */
internal class RecordSyncLease(private val store: KeystoreTokenStore, private val token: StoredToken,
  private val deadline: Instant, private val clock: Clock = Clock.systemUTC()) {
  fun permits(accountId: String): Boolean = try {
    val now = clock.instant()
    val current = if (store.isLogoutPending()) null else store.read()
    val permit = current?.cacheAuthorization
    current?.accessToken == token.accessToken && permit?.accountId == accountId &&
      token.cacheAuthorization?.accountId == accountId && permit.permits(now) && now < deadline
  } catch (_: Exception) { false }
}
