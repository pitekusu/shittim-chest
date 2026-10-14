package dev.pitekusu.shittim.records

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator

internal const val RECORD_NOTIFICATION_CHANNEL = "record-published-v1"
internal const val RECORD_NOTIFICATION_VIBRATION_CHANNEL = "record-published-v2"

// One pulse, a pause, then two shorter pulses: 「ブッ、ブブ」. Never repeat.
private fun recordVibrationTimings() = longArrayOf(0, 250, 250, 150, 100, 150)

/** Build before registration; Android makes channel vibration immutable afterwards. */
internal fun newRecordVibrationChannel(id: String, name: String,
  previous: NotificationChannel? = null, amplitudeControl: Boolean = false): NotificationChannel =
  NotificationChannel(id, name, previous?.importance ?: NotificationManager.IMPORTANCE_DEFAULT).apply {
    if (previous != null) {
      description = previous.description
      group = previous.group
      setSound(previous.sound, previous.audioAttributes)
      setShowBadge(previous.canShowBadge())
      lockscreenVisibility = previous.lockscreenVisibility
      enableLights(previous.shouldShowLights())
      lightColor = previous.lightColor
      setBypassDnd(previous.canBypassDnd())
      if (Build.VERSION.SDK_INT >= 29) setAllowBubbles(previous.canBubble())
      if (Build.VERSION.SDK_INT >= 33) setBlockable(previous.isBlockable)
    }
    val timings = recordVibrationTimings()
    // Only API 35+ channels support amplitude. Otherwise preserve the same on/off rhythm.
    if (Build.VERSION.SDK_INT >= 35 && amplitudeControl) {
      vibrationEffect = VibrationEffect.createWaveform(timings, intArrayOf(0, 255, 0, 255, 0, 255), -1)
    } else vibrationPattern = timings
  }

internal fun activeRecordNotificationChannelId(context: Context): String {
  val system = context.getSystemService(NotificationManager::class.java)
  return if (RecordNotificationSettings(context).vibrationChannelSelected ||
    system.getNotificationChannel(RECORD_NOTIFICATION_CHANNEL) == null) RECORD_NOTIFICATION_VIBRATION_CHANNEL
  else RECORD_NOTIFICATION_CHANNEL
}

internal fun needsVibrationUpgrade(context: Context): Boolean =
  activeRecordNotificationChannelId(context) == RECORD_NOTIFICATION_CHANNEL

internal fun ensureRecordNotificationChannel(context: Context): NotificationChannel {
  val system = context.getSystemService(NotificationManager::class.java)
  val id = activeRecordNotificationChannelId(context)
  system.getNotificationChannel(id)?.let { return it }
  val channel = newRecordVibrationChannel(id, context.getString(R.string.notification_channel),
    amplitudeControl = context.getSystemService(Vibrator::class.java)?.hasAmplitudeControl() == true)
  system.createNotificationChannel(channel)
  return checkNotNull(system.getNotificationChannel(id))
}

/** Call only for an explicit user choice; old vibration OFF cannot be distinguished from the old default. */
internal fun upgradeRecordNotificationVibration(context: Context) = synchronized(RecordNotifications.lock) {
  if (!ReviewDemoModeStore.productionAllowed(context) || !needsVibrationUpgrade(context)) return@synchronized
  val system = context.getSystemService(NotificationManager::class.java)
  val previous = system.getNotificationChannel(RECORD_NOTIFICATION_CHANNEL) ?: return@synchronized
  // Never delete/recreate v2: its later user choices must survive subsequent app upgrades.
  if (system.getNotificationChannel(RECORD_NOTIFICATION_VIBRATION_CHANNEL) == null) {
    system.createNotificationChannel(newRecordVibrationChannel(RECORD_NOTIFICATION_VIBRATION_CHANNEL,
      context.getString(R.string.notification_channel), previous,
      context.getSystemService(Vibrator::class.java)?.hasAmplitudeControl() == true))
  }
  checkNotNull(system.getNotificationChannel(RECORD_NOTIFICATION_VIBRATION_CHANNEL))
  RecordNotificationSettings(context).selectVibrationChannel()
}
