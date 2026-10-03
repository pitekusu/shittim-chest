package dev.pitekusu.shittim.records

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.google.firebase.messaging.FirebaseMessaging
import com.google.firebase.installations.FirebaseInstallations
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.MobileAuthException
import dev.pitekusu.shittim.records.auth.MobileAuthFailure
import dev.pitekusu.shittim.records.auth.StoredToken
import java.time.Instant
import java.util.concurrent.TimeUnit
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext

internal object RecordNotificationRegistration {
  private const val WORK = "records-notification-registration-v1"
  fun schedule(context: Context, replace: Boolean = false) {
    if (!RecordNotifications.configured(context)) return
    WorkManager.getInstance(context).enqueueUniqueWork(WORK,
      if (replace) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.KEEP,
      OneTimeWorkRequestBuilder<RecordNotificationWorker>()
        .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
        .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
  }
  fun cancel(context: Context) { WorkManager.getInstance(context).cancelUniqueWork(WORK) }
}

/** Reads credentials only while running; WorkData never contains tokens, bindings or record text. */
internal class RecordNotificationWorker(context: Context, parameters: WorkerParameters) :
  CoroutineWorker(context, parameters) {
  override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
    val context = applicationContext
    val store = KeystoreTokenStore(context)
    var active: StoredToken? = null
    try {
      if (!RecordNotifications.configured(context)) return@withContext Result.success()
      val stored = if (store.isLogoutPending()) null else store.read()
      active = stored
      val permit = stored?.cacheAuthorization
      if (stored == null || stored.expiresAt <= Instant.now() || permit?.permits(Instant.now()) != true) {
        RecordNotifications.revoke(context)
        return@withContext Result.success()
      }
      val enabled = synchronized(RecordNotifications.lock) {
        !RecordNotifications.locallyRevoked && RecordNotificationSettings(context).optedIn && RecordNotifications.permitted(context)
      }
      val messaging = FirebaseMessaging.getInstance()
      // Persisted SDK auto-init could run before expiry/logout checks after a cold start.
      // Refresh explicitly in this authorized worker instead, including each foreground entry.
      messaging.isAutoInitEnabled = false
      if (!enabled) {
        // Disabling in Android settings is also a local opt-out and a server unregister.
        val binding = synchronized(RecordNotifications.lock) {
          val settings = RecordNotificationSettings(context)
          // An older worker must not disable a generation explicitly enabled meanwhile.
          if (!current(store, stored) || (settings.optedIn && RecordNotifications.permitted(context))) {
            return@synchronized null
          }
          settings.disable()
          settings.binding.takeIf { settings.sessionFingerprint == notificationSessionFingerprint(stored.accessToken) }
        } ?: return@withContext Result.success()
        val fcm = FirebaseInstallations.getInstance().id.await()
        if (current(store, stored)) MobileAuthClient().use {
          it.unregisterNotifications(stored.accessToken, fcm, binding)
        }
        // Local gating and server DELETE suffice; an asynchronous SDK unregister could finish
        // after a newer opt-in/account registration and disable that newer generation.
        return@withContext Result.success()
      }
      messaging.register().await()
      val fcm = FirebaseInstallations.getInstance().id.await()
      val binding = synchronized(RecordNotifications.lock) {
        val settings = RecordNotificationSettings(context)
        if (RecordNotifications.locallyRevoked || !current(store, stored) ||
          !settings.optedIn || !RecordNotifications.permitted(context)) null
        else settings.bindingFor(stored.accessToken, fcm)
      } ?: return@withContext Result.success()
      val started = Instant.now()
      val expires = MobileAuthClient().use { it.registerNotifications(stored.accessToken, fcm, binding) }
      check(expires > Instant.now() && expires <= stored.expiresAt)
      synchronized(RecordNotifications.lock) {
        if (current(store, stored) && RecordNotifications.permitted(context)) {
          RecordNotificationSettings(context).registered(binding, started, expires)
        }
      }
      Result.success()
    } catch (error: CancellationException) { throw error }
    catch (error: MobileAuthException) {
      if (error.failure in setOf(MobileAuthFailure.AUTHENTICATION_REQUIRED, MobileAuthFailure.FORBIDDEN)) {
        synchronized(RecordNotifications.lock) {
          if (active?.let { current(store, it) } == true) {
            store.invalidateCacheAuthorization(active.accessToken)
            RecordNotifications.revoke(context)
          }
        }
        Result.failure()
      } else retryOrFail()
    } catch (_: Exception) { retryOrFail() }
  }

  private fun current(store: KeystoreTokenStore, expected: StoredToken): Boolean = try {
    val now = Instant.now()
    val token = if (store.isLogoutPending()) null else store.read()
    token?.accessToken == expected.accessToken && token.expiresAt > now &&
      token.cacheAuthorization?.accountId == expected.cacheAuthorization?.accountId &&
      token.cacheAuthorization?.permits(now) == true
  } catch (_: Exception) { false }

  private fun retryOrFail(): Result = if (runAttemptCount < 3) Result.retry() else {
    synchronized(RecordNotifications.lock) { RecordNotificationSettings(applicationContext).failure() }
    Result.failure()
  }
}
