package dev.pitekusu.shittim.records

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

internal const val REVIEW_DEMO_NOTIFICATION_ACTION = "dev.pitekusu.shittim.records.REVIEW_DEMO_NOTIFICATION"
internal const val REVIEW_DEMO_NOTIFICATION_ENTRY = "review_demo_notification_entry"
internal const val REVIEW_DEMO_NOTIFICATION_RECORD = "review_demo_notification_record"
internal const val REVIEW_DEMO_NOTIFICATION_CHANNEL = "review-demo-results-v1"

internal fun demoNotificationIntent(context: Context, entryId: String, recordId: String): Intent =
  Intent(context, MainActivity::class.java).setAction(REVIEW_DEMO_NOTIFICATION_ACTION)
    .setData(Uri.Builder().scheme("shittim-demo").authority("notification").appendPath(entryId).appendPath(recordId).build())
    .putExtra(REVIEW_DEMO_NOTIFICATION_ENTRY, entryId).putExtra(REVIEW_DEMO_NOTIFICATION_RECORD, recordId)
    .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)

/** Local completion notifications have no Firebase registration or production credentials. */
internal object DemoNotifications {
  private val mutableChanges = MutableStateFlow(0L)
  val changes = mutableChanges.asStateFlow()
  private fun changed() { mutableChanges.update { it + 1 } }
  private fun preferences(context: Context) = context.getSharedPreferences("review-demo-notifications-v1", Context.MODE_PRIVATE)
  private fun current(context: Context, entryId: String) = ReviewDemoModeStore(context).isDemo(entryId)

  fun optedIn(context: Context, entryId: String): Boolean = synchronized(RecordNotifications.lock) {
    current(context, entryId) && preferences(context).let { it.getString("entry", null) == entryId && it.getBoolean("enabled", false) }
  }

  private fun permitted(context: Context): Boolean =
    (Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) &&
      NotificationManagerCompat.from(context).let { it.areNotificationsEnabled() && notificationChannelAllowed(it.getNotificationChannel(REVIEW_DEMO_NOTIFICATION_CHANNEL)) }

  fun status(context: Context, entryId: String): RecordNotificationStatus = when {
    !optedIn(context, entryId) -> RecordNotificationStatus.DISABLED
    !permitted(context) -> RecordNotificationStatus.PERMISSION_DENIED
    else -> RecordNotificationStatus.ENABLED
  }

  fun enable(context: Context, entryId: String) = synchronized(RecordNotifications.lock) {
    if (!current(context, entryId)) return@synchronized
    val settings = preferences(context)
    val editor = settings.edit().putString("entry", entryId).putBoolean("enabled", true)
    if (settings.getString("entry", null) != entryId) editor.remove("seen")
    check(editor.commit())
    changed()
  }

  fun disable(context: Context, entryId: String) = synchronized(RecordNotifications.lock) {
    if (!current(context, entryId)) return@synchronized
    check(preferences(context).edit().putString("entry", entryId).putBoolean("enabled", false).commit())
    cancelDisplayed(context)
    changed()
  }

  /** Denial is remembered independently, so a later demo entry does not repeat the OS dialog. */
  fun claimPermissionRequest(context: Context, entryId: String, alreadyGranted: Boolean): Boolean = synchronized(RecordNotifications.lock) {
    val settings = preferences(context)
    if (!current(context, entryId) || !optedIn(context, entryId) || Build.VERSION.SDK_INT < 33 || settings.getBoolean("permissionRequested", false)) return@synchronized false
    check(settings.edit().putBoolean("permissionRequested", true).commit())
    changed()
    !alreadyGranted
  }

  fun permissionResult(context: Context, entryId: String, granted: Boolean) = synchronized(RecordNotifications.lock) {
    if (!current(context, entryId)) return@synchronized
    check(preferences(context).edit().putBoolean("permissionRequested", true).putBoolean("permissionDenied", !granted).commit())
    changed()
  }

  fun refresh() { changed() }

  fun clear(context: Context) = synchronized(RecordNotifications.lock) {
    val settings = preferences(context)
    check(settings.edit().remove("entry").remove("enabled").remove("seen").commit())
    cancelDisplayed(context)
    changed()
  }

  private fun cancelDisplayed(context: Context) {
    val manager = context.getSystemService(NotificationManager::class.java)
    manager.activeNotifications.filter { it.notification.channelId == REVIEW_DEMO_NOTIFICATION_CHANNEL }
      .forEach { manager.cancel(it.tag, it.id) }
  }

  @SuppressLint("MissingPermission")
  fun show(context: Context, entryId: String, recordId: String, requesterName: String): Boolean = synchronized(RecordNotifications.lock) {
    try {
      if (!current(context, entryId) || !Regex("[A-Za-z0-9_-]{43}").matches(recordId) || !optedIn(context, entryId) || !permitted(context)) return@synchronized false
      val settings = preferences(context)
      val seen = settings.getString("seen", "")!!.split(',').filter { it.isNotEmpty() }
      if (recordId in seen) return@synchronized false
      val system = context.getSystemService(NotificationManager::class.java)
      system.createNotificationChannel(NotificationChannel(REVIEW_DEMO_NOTIFICATION_CHANNEL, context.getString(R.string.demo_notification_channel), NotificationManager.IMPORTANCE_DEFAULT))
      if (!permitted(context)) return@synchronized false
      val intent = demoNotificationIntent(context, entryId, recordId)
      val content = PendingIntent.getActivity(context, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
      val name = requesterName.filter { !it.isISOControl() }.take(100)
      val body = context.getString(R.string.demo_notification_body, name)
      val notification = NotificationCompat.Builder(context, REVIEW_DEMO_NOTIFICATION_CHANNEL)
        .setSmallIcon(R.drawable.ic_record_notification).setContentTitle(context.getString(R.string.demo_notification_title))
        .setContentText(body).setStyle(NotificationCompat.BigTextStyle().bigText(body))
        .setContentIntent(content).setAutoCancel(true).setOnlyAlertOnce(true)
        .setVisibility(NotificationCompat.VISIBILITY_PRIVATE).setCategory(NotificationCompat.CATEGORY_STATUS).build()
      check(settings.edit().putString("seen", (seen + recordId).takeLast(128).joinToString(",")).commit())
      NotificationManagerCompat.from(context).notify("review-demo-$entryId-$recordId", 1, notification)
      true
    } catch (_: Exception) { false }
  }
}
