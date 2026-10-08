package dev.pitekusu.shittim.records

import android.content.Context
import android.util.Log
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.ListenableWorker
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
import java.io.IOException
import java.time.Instant
import java.util.concurrent.TimeUnit
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.tasks.await
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

internal object RecordNotificationRegistration {
  private const val WORK = "records-notification-registration-v1"
  fun onRegistered(context: Context, installationId: String) {
    // register() itself emits this callback before the worker saves the new fingerprint.
    // KEEP lets that running worker finish; replacing it would cancel its own registration.
    val changed = synchronized(RecordNotifications.lock) {
      val settings = RecordNotificationSettings(context)
      settings.optedIn && settings.binding != null && settings.deliveryFingerprint !=
        notificationSessionFingerprint(installationId)
    }
    if (changed) schedule(context)
  }

  fun schedule(context: Context, replace: Boolean = false, afterCurrent: Boolean = false) {
    if (!RecordNotifications.configured(context)) return
    WorkManager.getInstance(context).enqueueUniqueWork(WORK,
      when {
        replace -> ExistingWorkPolicy.REPLACE
        afterCurrent -> ExistingWorkPolicy.APPEND_OR_REPLACE
        else -> ExistingWorkPolicy.KEEP
      },
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
    if (!RecordNotifications.configured(applicationContext)) Result.success()
    else registerRecordNotifications(applicationContext, runAttemptCount)
  }
}

/** The production SDK/API boundary is callable with synthetic inputs, without another DI layer. */
internal suspend fun registerRecordNotifications(context: Context, attempt: Int,
  client: () -> MobileAuthClient = { MobileAuthClient() },
  installation: suspend (Boolean) -> String = { register ->
    FirebaseMessaging.getInstance().also { messaging ->
      messaging.isAutoInitEnabled = false
      if (register) messaging.register().await()
    }
    FirebaseInstallations.getInstance().id.await()
  },
  clock: () -> Instant = Instant::now,
  firebaseTimeoutMillis: Long = 30_000,
): ListenableWorker.Result {
    val store = KeystoreTokenStore(context)
    var active: StoredToken? = null
    var stage = NotificationRegistrationStage.SESSION
    var expectedBinding = RecordNotificationSettings(context).binding
    var expectedSession = RecordNotificationSettings(context).sessionFingerprint
    try {
      val stored = if (store.isLogoutPending()) null else store.read()
      active = stored
      val permit = stored?.cacheAuthorization
      if (stored == null || stored.expiresAt <= clock() || permit?.permits(clock()) != true) {
        RecordNotifications.revoke(context)
        return ListenableWorker.Result.success()
      }
      val enabled = synchronized(RecordNotifications.lock) {
        !RecordNotifications.locallyRevoked && RecordNotificationSettings(context).optedIn && RecordNotifications.permitted(context)
      }
      if (!enabled) {
        // OS denial suppresses delivery and unregisters without changing the app's ON/OFF choice.
        val binding = synchronized(RecordNotifications.lock) {
          val settings = RecordNotificationSettings(context)
          // An older worker must not disable a generation explicitly enabled meanwhile.
          if (!currentNotificationSession(store, stored, clock()) || (settings.optedIn && RecordNotifications.permitted(context))) {
            return@synchronized null
          }
          settings.clearRegistration()
          settings.binding.takeIf { settings.sessionFingerprint == notificationSessionFingerprint(stored.accessToken) }
        } ?: return ListenableWorker.Result.success()
        stage = NotificationRegistrationStage.FIREBASE
        val fcm = withTimeoutOrNull(firebaseTimeoutMillis) { installation(false) }
          ?: throw IOException("notification_firebase_timeout")
        stage = NotificationRegistrationStage.UNREGISTER
        if (currentNotificationSession(store, stored, clock())) client().use {
          it.unregisterNotifications(stored.accessToken, fcm, binding)
        }
        // Local gating and server DELETE suffice; an asynchronous SDK unregister could finish
        // after a newer opt-in/account registration and disable that newer generation.
        return ListenableWorker.Result.success()
      }
      stage = NotificationRegistrationStage.FIREBASE
      val fcm = withTimeoutOrNull(firebaseTimeoutMillis) { installation(true) }
        ?: throw IOException("notification_firebase_timeout")
      stage = NotificationRegistrationStage.SAVE
      val binding = synchronized(RecordNotifications.lock) {
        val settings = RecordNotificationSettings(context)
        if (RecordNotifications.locallyRevoked || !currentNotificationSession(store, stored, clock()) ||
          !settings.optedIn || !RecordNotifications.permitted(context)) null
        else settings.bindingFor(stored.accessToken, fcm)
      } ?: return ListenableWorker.Result.success()
      expectedBinding = binding
      expectedSession = notificationSessionFingerprint(stored.accessToken)
      val started = clock()
      stage = NotificationRegistrationStage.REGISTER
      val serverExpiry = client().use { it.registerNotifications(stored.accessToken, fcm, binding) }
      stage = NotificationRegistrationStage.EXPIRY
      // Local verification may conservatively shorten the server's absolute deadline.
      // Cap local delivery instead of rejecting a valid registration or extending a permit.
      val expires = minOf(serverExpiry, stored.expiresAt, checkNotNull(permit).expiresAt)
      check(expires > clock())
      stage = NotificationRegistrationStage.SAVE
      synchronized(RecordNotifications.lock) {
        if (currentNotificationSession(store, stored, clock()) && RecordNotifications.permitted(context)) {
          RecordNotificationSettings(context).registered(binding, started, expires)
        }
      }
      return ListenableWorker.Result.success()
    } catch (error: CancellationException) { throw error }
    catch (error: Exception) {
      val category = notificationRegistrationFailure(stage, error)
      val retry = category.retryable && attempt < 3
      // Enums and a bounded counter are the entire diagnostic payload, including release builds.
      Log.w("RecordNotifications", "registration_failed stage=${stage.name} category=${category.name} attempt=${(attempt + 1).coerceIn(1, 4)} retry=$retry")
      if (error is MobileAuthException && error.failure in setOf(MobileAuthFailure.AUTHENTICATION_REQUIRED, MobileAuthFailure.FORBIDDEN)) {
        synchronized(RecordNotifications.lock) {
          if (active?.let { currentNotificationSession(store, it, clock()) } == true) {
            store.invalidateCacheAuthorization(active.accessToken)
            RecordNotifications.revoke(context)
          }
        }
        return ListenableWorker.Result.failure()
      }
      synchronized(RecordNotifications.lock) {
        val settings = RecordNotificationSettings(context)
        // A cancelled/replaced registration must not mark a newer binding or OFF setting failed.
        if (!RecordNotifications.locallyRevoked && settings.optedIn && RecordNotifications.permitted(context) &&
          settings.binding == expectedBinding && settings.sessionFingerprint == expectedSession) {
          settings.failure(stage, category, attempt + 1, terminal = !retry)
        }
      }
      return if (retry) ListenableWorker.Result.retry() else ListenableWorker.Result.failure()
    }
}

private fun currentNotificationSession(store: KeystoreTokenStore, expected: StoredToken, now: Instant): Boolean = try {
  val token = if (store.isLogoutPending()) null else store.read()
  token?.accessToken == expected.accessToken && token.expiresAt > now &&
    token.cacheAuthorization?.accountId == expected.cacheAuthorization?.accountId &&
    token.cacheAuthorization?.permits(now) == true
} catch (_: Exception) { false }
