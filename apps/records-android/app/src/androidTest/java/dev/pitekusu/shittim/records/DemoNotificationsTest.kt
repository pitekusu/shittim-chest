package dev.pitekusu.shittim.records

import android.Manifest
import android.app.NotificationManager
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.work.ListenableWorker
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.StoredToken
import java.io.File
import java.nio.file.Files
import java.time.Instant
import java.util.UUID
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DemoNotificationsTest {
  private lateinit var context: Context
  private lateinit var notificationContext: Context
  private lateinit var directory: File
  private val entry = UUID.randomUUID().toString()
  private val record = "d".repeat(43)
  private val now = Instant.parse("2030-01-01T00:00:00Z")

  @Before fun setup() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "demo-notifications-test-").toFile()
    notificationContext = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir() = directory
      override fun getPackageName() = app.packageName
      override fun getSharedPreferences(name: String, mode: Int) = app.getSharedPreferences("${directory.name}-$name", mode)
    }
    context = object : ContextWrapper(notificationContext) {
      override fun getPackageName() = "${app.packageName}.demo.notifications.${directory.name}"
    }
    ReviewDemoModeStore(context).write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
  }

  @After fun cleanup() {
    DemoNotifications.clear(context)
    KeystoreTokenStore(context).clear()
    KeystoreTokenStore(context).completeLogout()
    for (name in listOf("record-notification-v1", "review-demo-notifications-v1"))
      context.getSharedPreferences(name, Context.MODE_PRIVATE).edit().clear().commit()
    context.getSystemService(NotificationManager::class.java).deleteNotificationChannel(REVIEW_DEMO_NOTIFICATION_CHANNEL)
    directory.deleteRecursively()
  }

  @Test fun demoDoesNotReadAProductionSessionRegisterFirebaseOrCallAnApi() = runBlocking {
    KeystoreTokenStore(context).save(StoredToken("t".repeat(43), now.plusSeconds(3600),
      CacheAuthorization("a".repeat(43), now.minusSeconds(60), now.plusSeconds(3600))))
    val result = registerRecordNotifications(context, 0,
      client = { error("demo_must_not_create_a_production_auth_client") },
      installation = { error("demo_must_not_register_firebase") }, clock = { now })
    assertEquals(ListenableWorker.Result.success(), result)
    assertFalse(hasLocalNotificationPermit(context))
    assertFalse(showRecordNotification(context, emptyMap()))
    RecordNotificationRegistration.onRegistered(context, "synthetic-installation-id")
    assertTrue(context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE).all.isEmpty())
  }

  @Test fun localOptInAndNotificationDestinationAreBoundToOneDemoEntry() = runBlocking {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
    if (app.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
      automation.grantRuntimePermission(app.packageName, Manifest.permission.POST_NOTIFICATIONS)
    RecordNotificationSettings(context).disable()
    assertFalse(DemoNotifications.optedIn(context, entry))
    DemoNotifications.enable(context, entry)
    assertTrue(DemoNotifications.optedIn(context, entry))
    assertFalse(RecordNotificationSettings(context).optedIn)
    val intent = demoNotificationIntent(context, entry, record)
    assertEquals(MainActivity::class.java.name, intent.component?.className)
    assertEquals(REVIEW_DEMO_NOTIFICATION_ACTION, intent.action)
    assertEquals("shittim-demo", intent.data?.scheme)
    assertEquals(entry, intent.getStringExtra(REVIEW_DEMO_NOTIFICATION_ENTRY))
    assertEquals(record, intent.getStringExtra(REVIEW_DEMO_NOTIFICATION_RECORD))
    assertFalse(intent.hasExtra(RECORD_NOTIFICATION_BINDING))
    assertTrue(DemoNotifications.show(notificationContext, entry, record, "架空の依頼者"))
    assertFalse(DemoNotifications.show(notificationContext, entry, record, "架空の依頼者"))
    val manager = context.getSystemService(NotificationManager::class.java)
    withTimeout(5_000) {
      while (manager.activeNotifications.none { it.notification.channelId == REVIEW_DEMO_NOTIFICATION_CHANNEL }) delay(20)
    }
    ReviewDemoModeStore(context).write(ReviewDemoMode(ReviewDemoPhase.RESETTING, entry))
    assertFalse(DemoNotifications.show(notificationContext, entry, "e".repeat(43), "架空の依頼者"))
    DemoNotifications.clear(context)
    withTimeout(5_000) {
      while (manager.activeNotifications.any { it.notification.channelId == REVIEW_DEMO_NOTIFICATION_CHANNEL }) delay(20)
    }
    val next = UUID.randomUUID().toString()
    ReviewDemoModeStore(context).write(ReviewDemoMode(ReviewDemoPhase.DEMO, next))
    assertFalse(DemoNotifications.optedIn(context, next))
    assertFalse(DemoNotifications.show(notificationContext, entry, record, "架空の依頼者"))
    assertFalse(RecordNotificationSettings(context).optedIn)
  }

  @Test fun repeatedDemoEntriesKeepAnOsDenialWithoutChangingTheProductionPreference() {
    DemoNotifications.enable(context, entry)
    assertTrue(DemoNotifications.claimPermissionRequest(context, entry, alreadyGranted = false))
    DemoNotifications.permissionResult(context, entry, false)
    assertFalse(DemoNotifications.claimPermissionRequest(context, entry, alreadyGranted = false))
    DemoNotifications.clear(context)
    val next = UUID.randomUUID().toString()
    ReviewDemoModeStore(context).write(ReviewDemoMode(ReviewDemoPhase.DEMO, next))
    DemoNotifications.enable(context, next)
    assertFalse(DemoNotifications.claimPermissionRequest(context, next, alreadyGranted = false))
    assertFalse(RecordNotificationSettings(context).permissionRequested)
  }

  @Test fun aLateFirebaseResultCannotRegisterAfterADemoRoundTrip() = runBlocking {
    ReviewDemoModeStore(context).write(ReviewDemoMode(ReviewDemoPhase.NORMAL))
    KeystoreTokenStore(context).save(StoredToken("t".repeat(43), now.plusSeconds(3600),
      CacheAuthorization("a".repeat(43), now.minusSeconds(60), now.plusSeconds(3600))))
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    if (app.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
      InstrumentationRegistry.getInstrumentation().uiAutomation.grantRuntimePermission(app.packageName, Manifest.permission.POST_NOTIFICATIONS)
    RecordNotifications.locallyRevoked = false
    val result = registerRecordNotifications(context, 0,
      client = { error("stale_firebase_result_must_not_reach_the_production_api") }, installation = {
        ReviewDemoModeStore(context).write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
        ReviewDemoModeStore(context).write(ReviewDemoMode(ReviewDemoPhase.NORMAL))
        "synthetic-installation-id"
      }, clock = { now })
    assertEquals(ListenableWorker.Result.success(), result)
    assertNull(RecordNotificationSettings(context).binding)
    assertNull(RecordNotificationSettings(context).registeredAt)
  }
}
