package dev.pitekusu.shittim.records

import android.Manifest
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

/** Only opt-in, opaque binding and bounded dedup IDs are saved; never auth/FCM tokens or text. */
internal class RecordNotificationSettings(context: Context) {
  private val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
  val optedIn: Boolean get() = preferences.getBoolean("enabled", false)
  val binding: String? get() = preferences.getString("binding", null)
  val sessionFingerprint: String? get() = preferences.getString("session", null)
  val deliveryFingerprint: String? get() = preferences.getString("fcm", null)
  val registeredAt: Instant? get() = date("registered")
  val expiresAt: Instant? get() = date("expires")
  val failed: Boolean get() = preferences.getBoolean("failed", false)
  val permissionDenied: Boolean get() = preferences.getBoolean("permissionDenied", false)

  fun optIn() {
    // Explicitly re-enabling always creates a new generation, rejecting older queued messages.
    check(preferences.edit().clear().putBoolean("enabled", true).commit())
    RecordNotifications.changed()
  }

  fun disable() {
    check(preferences.edit().putBoolean("enabled", false).remove("registered").remove("expires").commit())
    RecordNotifications.changed()
  }

  fun denyPermission() {
    check(preferences.edit().putBoolean("permissionDenied", true).commit())
    RecordNotifications.changed()
  }

  fun revoke() {
    check(preferences.edit().clear().commit())
    RecordNotifications.changed()
  }

  fun bindingFor(session: String, fcmToken: String): String {
    val fingerprint = notificationSessionFingerprint(session)
    val fcmFingerprint = notificationSessionFingerprint(fcmToken)
    if (binding != null && sessionFingerprint == fingerprint &&
      preferences.getString("fcm", null) == fcmFingerprint) return binding!!
    val next = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(32).also(SecureRandom()::nextBytes))
    check(preferences.edit().putString("binding", next).putString("session", fingerprint)
      .putString("fcm", fcmFingerprint).remove("registered").remove("expires").remove("seen")
      .putBoolean("failed", false).commit())
    RecordNotifications.changed()
    return next
  }

  fun registered(bindingId: String, at: Instant, expires: Instant) {
    if (!optedIn || binding != bindingId) return
    check(preferences.edit().putLong("registered", registeredAt?.epochSecond ?: at.epochSecond)
      .putLong("expires", expires.epochSecond).putBoolean("failed", false).commit())
    RecordNotifications.changed()
  }

  fun failure() {
    check(preferences.edit().putBoolean("failed", true).commit())
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
      NotificationManagerCompat.from(context).areNotificationsEnabled()

  fun status(context: Context): RecordNotificationStatus = synchronized(lock) {
    val settings = RecordNotificationSettings(context)
    when {
      !configured(context) -> RecordNotificationStatus.PREPARING
      settings.permissionDenied && !permitted(context) -> RecordNotificationStatus.PERMISSION_DENIED
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
    if (!configured(context) || !permitted(context)) return@synchronized
    RecordNotificationSettings(context).optIn()
    locallyRevoked = false
    RecordNotificationRegistration.schedule(context, replace = true)
  }

  fun disable(context: Context) = synchronized(lock) {
    RecordNotificationSettings(context).disable()
    NotificationManagerCompat.from(context).cancelAll()
    if (configured(context)) FirebaseMessaging.getInstance().isAutoInitEnabled = false
    RecordNotificationRegistration.schedule(context, replace = true)
  }
}
