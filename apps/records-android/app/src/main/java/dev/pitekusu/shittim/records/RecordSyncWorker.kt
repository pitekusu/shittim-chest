package dev.pitekusu.shittim.records

import android.content.Context
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
import kotlinx.coroutines.Dispatchers
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

  override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
    var observedToken: StoredToken? = null
    try {
      val stored = if (store.isLogoutPending()) null else store.read()
      observedToken = stored
      val permit = stored?.cacheAuthorization
      if (stored == null || permit == null || !permit.permits(Instant.now())) {
        return@withContext stopped(RecordReadFailure.AUTH_REQUIRED)
      }
      val verified = verifySession(stored.accessToken)
      if (verified.cacheAccountId != permit.accountId || !verified.expiresAt.isAfter(Instant.now())) {
        store.invalidateCacheAuthorization(stored.accessToken)
        return@withContext stopped(RecordReadFailure.AUTH_REQUIRED)
      }
      val lease = RecordSyncLease(store, stored, minOf(permit.expiresAt, verified.expiresAt))
      // A worker must not reactivate an owner after logout/account switching erased it.
      openRepository(lease).use { records ->
        val finished = withTimeoutOrNull(8 * 60_000L) {
          records.synchronize(stored.accessToken, permit.accountId) {
            setProgress(workDataOf("updatedAt" to System.currentTimeMillis()))
          }
          true
        }
        if (finished != true) return@withContext retryOrStop(RecordReadFailure.UNAVAILABLE)
      }
      Result.success(workDataOf("finishedAt" to System.currentTimeMillis()))
    } catch (error: CancellationException) { throw error }
    catch (error: MobileAuthException) {
      if (error.failure in setOf(MobileAuthFailure.AUTHENTICATION_REQUIRED, MobileAuthFailure.FORBIDDEN)) {
        try { observedToken?.let { store.invalidateCacheAuthorization(it.accessToken) } }
        catch (_: Exception) { return@withContext stopped(RecordReadFailure.STORAGE_UNAVAILABLE) }
        stopped(RecordReadFailure.AUTH_REQUIRED)
      } else retryOrStop(if (error.failure in setOf(MobileAuthFailure.NETWORK,
        MobileAuthFailure.UNAVAILABLE, MobileAuthFailure.THROTTLED)) RecordReadFailure.UNAVAILABLE
        else RecordReadFailure.INVALID_RESPONSE)
    } catch (error: RecordReadException) {
      if (error.failure == RecordReadFailure.AUTH_REQUIRED) {
        try { observedToken?.let { store.invalidateCacheAuthorization(it.accessToken) } }
        catch (_: Exception) { return@withContext stopped(RecordReadFailure.STORAGE_UNAVAILABLE) }
      }
      retryOrStop(error.failure)
    }
    catch (_: Exception) { stopped(RecordReadFailure.STORAGE_UNAVAILABLE) }
  }

  private fun retryOrStop(reason: RecordReadFailure): Result =
    if (reason == RecordReadFailure.UNAVAILABLE && runAttemptCount < 3) Result.retry() else stopped(reason)

  private fun stopped(reason: RecordReadFailure): Result = Result.failure(workDataOf(
    "failure" to reason.name, "finishedAt" to System.currentTimeMillis()))
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
