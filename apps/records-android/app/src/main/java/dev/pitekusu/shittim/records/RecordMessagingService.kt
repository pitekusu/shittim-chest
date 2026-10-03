package dev.pitekusu.shittim.records

import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.RECORDS_ORIGIN
import java.time.Instant

internal const val RECORD_NOTIFICATION_BINDING = "record_notification_binding"
internal const val RECORD_NOTIFICATION_CHANNEL = "record-published-v1"

internal fun recordNotificationIntent(context: Context, hint: RecordPublishedHint): Intent =
  Intent(context, MainActivity::class.java).setAction(Intent.ACTION_VIEW)
    .setData(Uri.parse("$RECORDS_ORIGIN/records/${hint.recordId}"))
    .putExtra(RECORD_NOTIFICATION_BINDING, hint.bindingId)
    .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)

class RecordMessagingService : FirebaseMessagingService() {
  override fun onRegistered(installationId: String) =
    RecordNotificationRegistration.onRegistered(this, installationId)

  override fun onDeletedMessages() {
    if (hasLocalNotificationPermit(this)) RecordSyncScheduler.syncNow(this)
  }

  override fun onMessageReceived(message: RemoteMessage) {
    // Server must send data-only; notification payloads bypass app gating in the background.
    if (message.notification != null) return
    if (showRecordNotification(this, message.data)) RecordSyncScheduler.syncNow(this)
  }
}

internal fun hasLocalNotificationPermit(context: Context): Boolean = synchronized(RecordNotifications.lock) {
  try {
    val settings = RecordNotificationSettings(context)
    val store = KeystoreTokenStore(context)
    val token = if (store.isLogoutPending()) null else store.read()
    !RecordNotifications.locallyRevoked && settings.optedIn && RecordNotifications.permitted(context) &&
      settings.sessionFingerprint == token?.accessToken?.let(::notificationSessionFingerprint) &&
      token?.cacheAuthorization?.permits(Instant.now()) == true && token.expiresAt > Instant.now()
  } catch (_: Exception) { false }
}

// Permission is rechecked together with the live session under the revocation lock.
@SuppressLint("MissingPermission")
internal fun showRecordNotification(context: Context, data: Map<String, String>): Boolean =
  synchronized(RecordNotifications.lock) {
    try {
      if (RecordNotifications.locallyRevoked) return@synchronized false
      val hint = RecordPublishedHint.parse(data) ?: return@synchronized false
      val settings = RecordNotificationSettings(context)
      val store = KeystoreTokenStore(context)
      val pendingLogout = store.isLogoutPending()
      val token = if (pendingLogout) null else store.read()
      if (!notificationAllowed(hint, token, settings.optedIn, RecordNotifications.permitted(context),
          settings.binding, settings.sessionFingerprint, settings.registeredAt, settings.expiresAt,
          pendingLogout, Instant.now()) || settings.seen(hint.recordId)) return@synchronized false
      val system = context.getSystemService(NotificationManager::class.java)
      system.createNotificationChannel(NotificationChannel(RECORD_NOTIFICATION_CHANNEL,
        context.getString(R.string.notification_channel), NotificationManager.IMPORTANCE_DEFAULT))
      if (system.getNotificationChannel(RECORD_NOTIFICATION_CHANNEL)?.importance == NotificationManager.IMPORTANCE_NONE) {
        return@synchronized false
      }
      val content = PendingIntent.getActivity(context, 0, recordNotificationIntent(context, hint),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
      val body = context.getString(R.string.notification_body, hint.requesterName)
      val notification = NotificationCompat.Builder(context, RECORD_NOTIFICATION_CHANNEL)
        .setSmallIcon(R.drawable.ic_record_notification)
        .setContentTitle(context.getString(R.string.notification_title))
        .setContentText(body).setStyle(NotificationCompat.BigTextStyle().bigText(body))
        .setContentIntent(content).setAutoCancel(true).setOnlyAlertOnce(true)
        .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
        .setCategory(NotificationCompat.CATEGORY_STATUS).build()
      // Commit the bounded dedup key first; disk failure must not allow repeated audible alerts.
      settings.remember(hint.recordId)
      NotificationManagerCompat.from(context).notify(hint.recordId, 1, notification)
      true
    } catch (_: Exception) { false } // No SDK/token/payload/error body logging.
  }
