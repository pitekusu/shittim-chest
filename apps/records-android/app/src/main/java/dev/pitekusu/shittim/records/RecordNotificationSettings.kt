package dev.pitekusu.shittim.records

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging
import java.security.SecureRandom
import java.time.Instant
import java.util.Base64
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

internal enum class RecordNotificationStatus { PREPARING, DISABLED, PERMISSION_DENIED, REGISTERING, ENABLED, FAILED }

internal fun notificationChannelAllowed(channel: NotificationChannel?): Boolean =
  channel?.importance != NotificationManager.IMPORTANCE_NONE

/** Only device choices, opaque binding and bounded dedup IDs are saved; never tokens or text. */
internal class RecordNotificationSettings(context: Context) {
  private val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
  // An absent preference is ON; a user-selected OFF is never reset on resume/login.
  val optedIn: Boolean get() = preferences.getBoolean("enabled", true)
  val binding: String? get() = preferences.getString("binding", null)
  val sessionFingerprint: String? get() = preferences.getString("session", null)
  val deliveryFingerprint: String? get() = preferences.getString("fcm", null)
  val registeredAt: Instant? get() = date("registered")
  val expiresAt: Instant? get() = date("expires")
  val failed: Boolean get() = preferences.getBoolean("failed", false)
  val failureStage: NotificationRegistrationStage? get() = NotificationRegistrationStage.entries
    .firstOrNull { it.name == preferences.getString("failureStage", null) }
  val failureCategory: NotificationRegistrationFailure? get() = NotificationRegistrationFailure.entries
    .firstOrNull { it.name == preferences.getString("failureCategory", null) }
  val failureAttempt: Int get() = preferences.getInt("failureAttempt", 1).coerceIn(1, 4)
  val permissionDenied: Boolean get() = preferences.getBoolean("permissionDenied", false)
  val permissionRequested: Boolean get() = preferences.getBoolean("permissionRequested", false) || permissionDenied

  fun optIn() {
    // Explicitly re-enabling always creates a new generation, rejecting older queued messages.
    check(clearBinding(preferences.edit()).putBoolean("enabled", true).commit())
    RecordNotifications.changed()
  }

  fun disable() {
    check(preferences.edit().putBoolean("enabled", false).remove("registered").remove("expires").commit())
    RecordNotifications.changed()
  }

  fun denyPermission() {
    permissionResult(false)
  }

  fun permissionResult(granted: Boolean) {
    check(preferences.edit().putBoolean("permissionRequested", true).putBoolean("permissionDenied", !granted).commit())
    RecordNotifications.changed()
  }

  /** Commit before launching the OS dialog, so cancellation/rotation never requests it twice. */
  fun claimPermissionRequest(eligible: Boolean, alreadyGranted: Boolean): Boolean {
    if (!eligible || !optedIn || permissionRequested) return false
    check(preferences.edit().putBoolean("permissionRequested", true).commit())
    RecordNotifications.changed()
    return !alreadyGranted
  }

  fun clearRegistration() {
    check(clearFailure(preferences.edit()).remove("registered").remove("expires").commit())
    RecordNotifications.changed()
  }

  fun revoke() {
    // Session cleanup must not undo an explicit OFF or repeat an already-dismissed OS dialog.
    check(clearBinding(preferences.edit()).commit())
    RecordNotifications.changed()
  }

  private fun clearBinding(editor: android.content.SharedPreferences.Editor) = editor
    .remove("binding").remove("session").remove("fcm").remove("registered").remove("expires")
    .remove("seen").let(::clearFailure)

  private fun clearFailure(editor: android.content.SharedPreferences.Editor) = editor
    .remove("failureStage").remove("failureCategory").remove("failureAttempt").remove("failed")

  fun retryRegistration() {
    check(clearFailure(preferences.edit()).commit())
    RecordNotifications.changed()
  }

  fun bindingFor(session: String, fcmToken: String): String {
    val fingerprint = notificationSessionFingerprint(session)
    val fcmFingerprint = notificationSessionFingerprint(fcmToken)
    if (binding != null && sessionFingerprint == fingerprint &&
      preferences.getString("fcm", null) == fcmFingerprint) return binding!!
    val next = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(32).also(SecureRandom()::nextBytes))
    check(clearFailure(preferences.edit()).putString("binding", next).putString("session", fingerprint)
      .putString("fcm", fcmFingerprint).remove("registered").remove("expires").remove("seen")
      .commit())
    RecordNotifications.changed()
    return next
  }

  fun registered(bindingId: String, at: Instant, expires: Instant) {
    if (!optedIn || binding != bindingId) return
    check(clearFailure(preferences.edit()).putLong("registered", registeredAt?.epochSecond ?: at.epochSecond)
      .putLong("expires", expires.epochSecond).commit())
    RecordNotifications.changed()
  }

  fun failure(stage: NotificationRegistrationStage, category: NotificationRegistrationFailure,
    attempt: Int, terminal: Boolean) {
    check(preferences.edit().putBoolean("failed", terminal).putString("failureStage", stage.name)
      .putString("failureCategory", category.name).putInt("failureAttempt", attempt.coerceIn(1, 4)).commit())
    RecordNotifications.changed()
  }

  fun seen(recordId: String): Boolean = recordId in preferences.getString("seen", "")!!.split(',')
  fun remember(recordId: String) {
    val ids = preferences.getString("seen", "")!!.split(',').filter { it.isNotEmpty() && it != recordId }
    check(preferences.edit().putString("seen", (ids + recordId).takeLast(128).joinToString(",")).commit())
  }
  private fun date(key: String): Instant? = preferences.getLong(key, 0).takeIf { it > 0 }?.let(Instant::ofEpochSecond)
}

internal object RecordNotifications {
  // Registration, display and local revocation are serialized in this app's one process.
  internal val lock = Any()
  private val mutableChanges = MutableStateFlow(0L)
  val changes = mutableChanges.asStateFlow()
  @Volatile internal var locallyRevoked = false
  internal fun changed() { mutableChanges.update { it + 1 } }

  fun configured(context: Context): Boolean = FirebaseApp.getApps(context).isNotEmpty()
  fun permitted(context: Context): Boolean =
    (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context,
      Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) &&
      NotificationManagerCompat.from(context).let { manager ->
        manager.areNotificationsEnabled() && notificationChannelAllowed(manager.getNotificationChannel(RECORD_NOTIFICATION_CHANNEL))
      }

  fun status(context: Context): RecordNotificationStatus = synchronized(lock) {
    val settings = RecordNotificationSettings(context)
    when {
      !configured(context) -> RecordNotificationStatus.PREPARING
      !settings.optedIn -> RecordNotificationStatus.DISABLED
      !permitted(context) -> RecordNotificationStatus.PERMISSION_DENIED
      settings.failed -> RecordNotificationStatus.FAILED
      settings.expiresAt?.isAfter(Instant.now()) == true -> RecordNotificationStatus.ENABLED
      else -> RecordNotificationStatus.REGISTERING
    }
  }

  fun revoke(context: Context) = synchronized(lock) {
    // Local suppression does not wait for API connectivity, worker completion or SDK IO.
    locallyRevoked = true
    // A notification-storage/SDK failure must never prevent credential/record logout cleanup.
    runCatching { RecordNotificationSettings(context).revoke() }
    runCatching { NotificationManagerCompat.from(context).cancelAll() }
    runCatching { RecordNotificationRegistration.cancel(context) }
    runCatching { if (configured(context)) FirebaseMessaging.getInstance().isAutoInitEnabled = false }
  }

  fun revokeMatchingSession(context: Context, accessToken: String) = synchronized(lock) {
    if (RecordNotificationSettings(context).sessionFingerprint == notificationSessionFingerprint(accessToken)) revoke(context)
  }

  fun enable(context: Context) = synchronized(lock) {
    if (!configured(context)) return@synchronized
    RecordNotificationSettings(context).optIn()
    RecordNotificationRegistration.schedule(context, replace = true)
  }

  fun retryRegistration(context: Context) = synchronized(lock) {
    RecordNotificationSettings(context).retryRegistration()
    RecordNotificationRegistration.schedule(context, replace = true)
  }

  fun resumeAuthorizedSession(context: Context) = synchronized(lock) {
    if (!configured(context)) return@synchronized
    locallyRevoked = false
    changed()
    // A foreground permission grant must not be lost behind an in-flight server DELETE.
    // Wait for that worker, rather than racing a replacement PUT with its old binding DELETE.
    val settings = RecordNotificationSettings(context)
    val registrationNeeded = settings.optedIn && permitted(context) &&
      settings.expiresAt?.isAfter(Instant.now()) != true
    RecordNotificationRegistration.schedule(context, afterCurrent = registrationNeeded)
  }

  fun permissionResult(context: Context, granted: Boolean) = synchronized(lock) {
    RecordNotificationSettings(context).permissionResult(granted)
    // Never re-enable an OFF setting or revive a session invalidated while the dialog was open.
    if (!locallyRevoked) RecordNotificationRegistration.schedule(context, replace = true)
  }

  fun disable(context: Context) = synchronized(lock) {
    RecordNotificationSettings(context).disable()
    NotificationManagerCompat.from(context).cancelAll()
    RecordNotificationRegistration.schedule(context, replace = true)
  }
}
