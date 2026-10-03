package dev.pitekusu.shittim.records

import android.content.Intent
import android.content.Context
import android.app.NotificationChannel
import android.app.NotificationManager
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.auth.StoredToken
import java.time.Duration
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordNotificationsTest {
  private val now = Instant.parse("2030-01-01T00:00:00Z")
  private val record = "r".repeat(43)
  private val binding = "b".repeat(43)
  private val account = "a".repeat(43)
  private val stored = StoredToken("t".repeat(43), now.plus(Duration.ofDays(1)),
    CacheAuthorization(account, now.minusSeconds(60), now.plus(Duration.ofDays(1))))
  private fun payload() = mapOf("type" to "record_published", "schemaVersion" to "1",
    "recordId" to record, "bindingId" to binding, "publishedAt" to now.toString(),
    "requesterName" to "架空の依頼者")
  private fun allowed(data: Map<String, String> = payload(), token: StoredToken? = stored,
    opted: Boolean = true, permission: Boolean = true, id: String = binding,
    fingerprint: String = notificationSessionFingerprint(stored.accessToken), pending: Boolean = false,
    registered: Instant = now.minusSeconds(30), expires: Instant = now.plusSeconds(60)): Boolean {
    val hint = RecordPublishedHint.parse(data) ?: return false
    return notificationAllowed(hint, token, opted, permission, id, fingerprint, registered, expires, pending, now)
  }

  @Test fun malformedUnknownOrRecordContentBearingPayloadCannotBecomeNotification() {
    assertTrue(allowed())
    assertNull(RecordPublishedHint.parse(payload() - "bindingId"))
    for ((key, value) in listOf("schemaVersion" to "2", "type" to "other", "recordId" to "../private",
      "bindingId" to "short", "publishedAt" to "2030-01-01T09:00:00+09:00", "publishedAt" to "not-a-date")) {
      assertFalse(allowed(payload() + (key to value)))
    }
    assertFalse(allowed(payload() + ("topic" to "must never display user text")))
    assertNull(RecordPublishedHint.parse(payload() - "requesterName"))
    for (name in listOf("", "name\nbody", "name\rbody", "name\u0000", "name\u2028body",
      " name ", "e\u0301", "name\uD800", "name\u202E", "あ".repeat(101))) {
      assertFalse(allowed(payload() + ("requesterName" to name)))
    }
    assertTrue(allowed(payload() + ("requesterName" to "🐟".repeat(100))))
    val hint = RecordPublishedHint.parse(payload())!!
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    assertEquals("依頼者：架空の依頼者\nタップして議論の記録を確認できます。",
      context.getString(R.string.notification_body, hint.requesterName))
  }

  @Test fun logoutRevokedPermissionsWrongAccountAndExpiredAuthorizationDropMessages() {
    assertFalse(allowed(token = null))
    assertFalse(allowed(opted = false))
    assertFalse(allowed(permission = false))
    assertFalse(allowed(id = "c".repeat(43)))
    assertFalse(allowed(fingerprint = "new-session"))
    assertFalse(allowed(pending = true))
    assertFalse(allowed(token = StoredToken(stored.accessToken, now)))
    assertFalse(allowed(token = StoredToken(stored.accessToken, now.plusSeconds(60))))
    assertFalse(allowed(expires = now))
    assertFalse(allowed(registered = now.plusSeconds(1)))
    assertFalse(allowed(payload() + ("publishedAt" to now.plusSeconds(61).toString())))
    assertFalse(allowed(payload() + ("publishedAt" to now.minus(Duration.ofDays(2)).toString()),
      registered = now.minus(Duration.ofDays(3))))
  }

  @Test fun notificationLinkIsExplicitContentFreeAndUsesExistingFixedOriginParser() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    val intent = recordNotificationIntent(context, RecordPublishedHint.parse(payload())!!)
    assertEquals(Intent.ACTION_VIEW, intent.action)
    assertEquals(MainActivity::class.java.name, intent.component?.className)
    assertEquals("/records/$record", recordDestination(intent.dataString))
    assertEquals(binding, intent.getStringExtra(RECORD_NOTIFICATION_BINDING))
    assertEquals(setOf(RECORD_NOTIFICATION_BINDING), intent.extras?.keySet())
    assertFalse(intent.toUri(0).contains(stored.accessToken))
  }

  @Test fun localBindingDedupIsBoundedAndRotatesAcrossSessionAndFcmGenerations() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    synchronized(RecordNotifications.lock) {
      val settings = RecordNotificationSettings(context)
      val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
      check(preferences.edit().clear().commit())
      try {
        settings.denyPermission()
        assertTrue(settings.permissionDenied)
        assertTrue(settings.optedIn)
        settings.optIn()
        assertTrue(settings.permissionRequested)
        val first = settings.bindingFor(stored.accessToken, "fake-fcm-token")
        assertEquals(43, first.length)
        assertEquals(first, settings.bindingFor(stored.accessToken, "fake-fcm-token"))
        settings.registered(first, now, now.plusSeconds(60))
        settings.remember(record)
        assertTrue(RecordNotificationSettings(context).seen(record))
        repeat(128) { settings.remember(it.toString().padStart(43, '0')) }
        assertFalse(settings.seen(record))
        val rotated = settings.bindingFor(stored.accessToken, "rotated-fake-fcm-token")
        assertNotEquals(first, rotated)
        assertNull(settings.registeredAt)
        assertNotEquals(rotated, settings.bindingFor("n".repeat(43), "rotated-fake-fcm-token"))
        settings.disable()
        assertFalse(settings.optedIn)
        assertNull(settings.expiresAt)
        settings.revoke()
        assertNull(settings.binding)
        assertNull(settings.sessionFingerprint)
      } finally { check(preferences.edit().clear().commit()) }
    }
  }

  @Test fun defaultOnRequestsPermissionOnceButExplicitOffSurvivesSessionCleanupAndRestart() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    synchronized(RecordNotifications.lock) {
      val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
      check(preferences.edit().clear().commit())
      val settings = RecordNotificationSettings(context)
      try {
        assertTrue(settings.optedIn)
        // Firebase absent, signed out or background are all ineligible; none consumes the request.
        assertFalse(settings.claimPermissionRequest(eligible = false, alreadyGranted = false))
        assertFalse(settings.permissionRequested)
        assertTrue(settings.claimPermissionRequest(eligible = true, alreadyGranted = false))
        // The attempt is durable even if the OS dialog is dismissed without granting permission.
        assertFalse(RecordNotificationSettings(context).claimPermissionRequest(true, false))
        settings.permissionResult(false)
        assertTrue(settings.optedIn)
        settings.disable()
        settings.permissionResult(true) // A late grant must not override a user's OFF selection.
        assertFalse(settings.optedIn)
        settings.revoke()
        val restored = RecordNotificationSettings(context)
        assertFalse(restored.optedIn)
        assertTrue(restored.permissionRequested)
        assertFalse(restored.claimPermissionRequest(true, false))
        restored.optIn()
        assertTrue(restored.optedIn)
        assertFalse(restored.claimPermissionRequest(true, false))
        restored.permissionResult(true)
        val first = restored.bindingFor(stored.accessToken, "fake-fid")
        restored.registered(first, now, now.plusSeconds(60))
        restored.clearRegistration() // OS permission revocation is not an app-side OFF.
        assertTrue(restored.optedIn)
        assertNull(restored.expiresAt)
        assertEquals(first, RecordNotificationSettings(context).bindingFor(stored.accessToken, "fake-fid"))
        restored.revoke()
        assertNull(restored.binding)
        assertTrue(restored.permissionRequested)
        assertTrue(restored.optedIn)
      } finally { check(preferences.edit().clear().commit()) }
    }
  }

  @Test fun preGrantedPermissionAndLegacyOffNeverTriggerAnAutomaticRequest() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    synchronized(RecordNotifications.lock) {
      val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
      check(preferences.edit().clear().commit())
      try {
        val settings = RecordNotificationSettings(context)
        assertFalse(settings.claimPermissionRequest(true, true))
        assertTrue(settings.permissionRequested)
        assertFalse(settings.claimPermissionRequest(true, false))
        check(preferences.edit().clear().putBoolean("enabled", false).commit())
        assertFalse(settings.optedIn)
        assertFalse(settings.claimPermissionRequest(true, false))
        check(preferences.edit().clear().putBoolean("permissionDenied", true).commit())
        assertTrue(settings.permissionRequested) // Existing denial migrates without a new dialog.
        assertFalse(settings.claimPermissionRequest(true, false))
      } finally { check(preferences.edit().clear().commit()) }
    }
  }

  @Test fun absentChannelIsAllowedButBlockedRecordChannelPreventsDelivery() {
    assertTrue(notificationChannelAllowed(null))
    assertTrue(notificationChannelAllowed(NotificationChannel(RECORD_NOTIFICATION_CHANNEL,
      "架空の議論結果", NotificationManager.IMPORTANCE_DEFAULT)))
    assertFalse(notificationChannelAllowed(NotificationChannel(RECORD_NOTIFICATION_CHANNEL,
      "架空の議論結果", NotificationManager.IMPORTANCE_NONE)))
    // Do not create a blocked OS channel: its user-controlled state cannot be reset by the app.
  }
}
