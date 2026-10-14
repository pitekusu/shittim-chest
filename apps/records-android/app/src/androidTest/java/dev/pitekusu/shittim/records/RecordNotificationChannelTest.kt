package dev.pitekusu.shittim.records

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.media.AudioAttributes
import android.os.Build
import android.os.VibrationEffect
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.util.UUID
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordNotificationChannelTest {
  @Test fun aNewChannelUsesOnePulseThenTwoPulsesWithoutRepeating() {
    val timings = longArrayOf(0, 250, 250, 150, 100, 150)
    val legacy = newRecordVibrationChannel("synthetic-pattern", "架空の議論結果")
    assertTrue(legacy.shouldVibrate())
    assertArrayEquals(timings, legacy.vibrationPattern)
    assertEquals(NotificationManager.IMPORTANCE_DEFAULT, legacy.importance)
    assertFalse(legacy.canBypassDnd())

    val amplitude = newRecordVibrationChannel("synthetic-amplitude", "架空の議論結果",
      amplitudeControl = true)
    assertTrue(amplitude.shouldVibrate())
    if (Build.VERSION.SDK_INT >= 35) {
      assertEquals(VibrationEffect.createWaveform(timings, intArrayOf(0, 255, 0, 255, 0, 255), -1),
        amplitude.vibrationEffect)
    } else assertArrayEquals(timings, amplitude.vibrationPattern)
  }

  @Test fun explicitVibrationUpgradePreservesSilentBlockedAndOtherChannelChoices() {
    val attributes = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION)
      .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
    for (importance in listOf(NotificationManager.IMPORTANCE_NONE, NotificationManager.IMPORTANCE_LOW)) {
      val previous = NotificationChannel("synthetic-old", "架空の議論結果", importance).apply {
        description = "架空の説明"
        group = "synthetic-group"
        setSound(null, attributes)
        setShowBadge(false)
        lockscreenVisibility = android.app.Notification.VISIBILITY_SECRET
        enableLights(true)
        lightColor = android.graphics.Color.BLUE
        enableVibration(false)
        setBypassDnd(false)
      }
      val upgraded = newRecordVibrationChannel("synthetic-new", "架空の議論結果", previous)
      assertEquals(importance, upgraded.importance)
      assertEquals(previous.description, upgraded.description)
      assertEquals(previous.group, upgraded.group)
      assertEquals(previous.sound, upgraded.sound)
      assertEquals(previous.audioAttributes, upgraded.audioAttributes)
      assertEquals(previous.canShowBadge(), upgraded.canShowBadge())
      assertEquals(previous.lockscreenVisibility, upgraded.lockscreenVisibility)
      assertEquals(previous.shouldShowLights(), upgraded.shouldShowLights())
      assertEquals(previous.lightColor, upgraded.lightColor)
      assertEquals(previous.canBypassDnd(), upgraded.canBypassDnd())
      assertTrue(upgraded.shouldVibrate())
      assertFalse(previous.shouldVibrate()) // Building the upgrade never edits the old channel.
      assertEquals(importance != NotificationManager.IMPORTANCE_NONE, notificationChannelAllowed(upgraded))
    }
  }

  @Test fun registeringTheSameChannelCannotResetAnExistingVibrationOffChoice() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    val system = context.getSystemService(NotificationManager::class.java)
    val id = "synthetic-record-vibration-${UUID.randomUUID()}"
    try {
      system.createNotificationChannel(NotificationChannel(id, "架空の議論結果",
        NotificationManager.IMPORTANCE_LOW).apply { enableVibration(false); setSound(null, null) })
      system.createNotificationChannel(newRecordVibrationChannel(id, "架空の議論結果",
        amplitudeControl = true))
      val retained = checkNotNull(system.getNotificationChannel(id))
      assertFalse(retained.shouldVibrate())
      assertEquals(NotificationManager.IMPORTANCE_LOW, retained.importance)
      assertEquals(null, retained.sound)
    } finally { system.deleteNotificationChannel(id) }
  }

  @Test fun selectedVibrationChannelSurvivesLogoutAppOffAndNewSessionBinding() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    synchronized(RecordNotifications.lock) {
      val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
      check(preferences.edit().clear().commit())
      try {
        val settings = RecordNotificationSettings(context)
        settings.selectVibrationChannel()
        settings.disable()
        settings.revoke()
        settings.bindingFor("synthetic-session", "synthetic-installation")
        val restored = RecordNotificationSettings(context)
        assertTrue(restored.vibrationChannelSelected)
        assertFalse(restored.optedIn)
        assertEquals(RECORD_NOTIFICATION_VIBRATION_CHANNEL, activeRecordNotificationChannelId(context))
        assertFalse(needsVibrationUpgrade(context))
      } finally { check(preferences.edit().clear().commit()) }
    }
  }
}
