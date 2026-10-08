package dev.pitekusu.shittim.records

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.work.ListenableWorker
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.StoredToken
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.time.Instant
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordNotificationWorkerTest {
  private val context = InstrumentationRegistry.getInstrumentation().targetContext
  private val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
  private val preferences get() = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
  private val settings get() = RecordNotificationSettings(context)
  private val store get() = KeystoreTokenStore(context)
  private val now = Instant.parse("2030-01-01T00:00:00Z")
  private val bearer = "t".repeat(43)

  @Before fun prepare() {
    val permissionGranted = context.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
    if (!permissionGranted) automation.grantRuntimePermission(context.packageName, Manifest.permission.POST_NOTIFICATIONS)
    check(preferences.edit().clear().commit())
    RecordNotifications.locallyRevoked = false
    store.clear()
    store.completeLogout()
    store.save(StoredToken(bearer, now.plusSeconds(3600),
      CacheAuthorization("a".repeat(43), now.minusSeconds(60), now.plusSeconds(1800))))
  }

  @After fun clean() {
    store.clear()
    store.completeLogout()
    check(preferences.edit().clear().commit())
    RecordNotifications.locallyRevoked = false
    // Android kills the instrumentation process when this permission is revoked.
    // Restore device permissions between test invocations, not from @After.
  }

  private suspend fun register(attempt: Int = 0, status: Int = 200,
    expiry: Instant = now.plusSeconds(3600), body: String? = null,
    onRequest: () -> Unit = {}): ListenableWorker.Result = registerRecordNotifications(context, attempt,
      client = { MobileAuthClient(MockEngine {
        onRequest()
        respond(body ?: """{"schemaVersion":1,"expiresAt":"$expiry"}""",
          HttpStatusCode.fromValue(status), headersOf("Content-Type", "application/json"))
      }) }, installation = { "synthetic-fid" }, clock = { now })

  @Test fun serverDeadlineAfterLocalCapStillCompletesRegistration() = runBlocking {
    // Local verification can conservatively shorten a server session, including clock skew.
    assertEquals(ListenableWorker.Result.success(), register(expiry = now.plusSeconds(3601)))
    assertEquals(now.plusSeconds(1800), settings.expiresAt)
    assertEquals(now, settings.registeredAt)
    assertFalse(settings.failed)
  }

  @Test fun expiredServerDeadlineIsTerminalWithoutExtendingLocalAuthorization() = runBlocking {
    assertEquals(ListenableWorker.Result.failure(), register(expiry = now))
    assertTrue(settings.failed)
    assertEquals(NotificationRegistrationStage.EXPIRY, settings.failureStage)
    assertEquals(NotificationRegistrationFailure.EXPIRY, settings.failureCategory)
    assertNull(settings.registeredAt)
  }

  @Test fun invalidResponseIsTerminalAndOnlyAllowlistedDiagnosticsSurviveRestart() = runBlocking {
    assertEquals(ListenableWorker.Result.failure(), register(body = "private-response-$bearer"))
    assertTrue(settings.failed)
    assertEquals(NotificationRegistrationStage.REGISTER, settings.failureStage)
    assertEquals(NotificationRegistrationFailure.RESPONSE, settings.failureCategory)
    assertEquals(1, settings.failureAttempt)
    for (privateValue in listOf(bearer, "synthetic-fid", "private-response")) {
      assertFalse(preferences.all.toString().contains(privateValue))
    }
    settings.retryRegistration()
    assertFalse(settings.failed)
    assertNull(settings.failureStage)
  }

  @Test fun transientFailureUsesExistingRetryBudgetAndSuccessClearsItsDiagnostic() = runBlocking {
    assertEquals(ListenableWorker.Result.retry(), register(status = 503))
    assertFalse(settings.failed)
    val binding = settings.binding
    assertEquals(NotificationRegistrationFailure.UNAVAILABLE, settings.failureCategory)
    assertEquals(ListenableWorker.Result.failure(), register(attempt = 3, status = 503))
    assertTrue(settings.failed)
    assertEquals(4, settings.failureAttempt)
    assertEquals(ListenableWorker.Result.success(), register())
    assertEquals(binding, settings.binding)
    assertFalse(settings.failed)
    assertNull(settings.failureCategory)
  }

  @Test fun staleFailureCannotOverwriteNewOptInAndLogoutCannotRestoreDelivery() = runBlocking {
    register(status = 503, onRequest = {
      settings.optIn()
      settings.bindingFor(bearer, "new-synthetic-fid")
    })
    assertFalse(settings.failed)
    assertNull(settings.failureCategory)
    register(onRequest = { RecordNotifications.revoke(context) })
    assertNull(settings.binding)
    assertNull(settings.registeredAt)
    assertTrue(RecordNotifications.locallyRevoked)
  }

  @Test fun firebaseWaitIsBoundedButCallerCancellationIsPreserved() = runBlocking {
    assertEquals(ListenableWorker.Result.retry(), registerRecordNotifications(context, 0,
      installation = { awaitCancellation() }, clock = { now }, firebaseTimeoutMillis = 1))
    assertEquals(NotificationRegistrationStage.FIREBASE, settings.failureStage)
    assertEquals(NotificationRegistrationFailure.NETWORK, settings.failureCategory)
    settings.retryRegistration()
    try {
      registerRecordNotifications(context, 0, installation = { throw CancellationException("synthetic cancellation") }, clock = { now })
      fail("expected cancellation")
    } catch (_: CancellationException) { }
    assertNull(settings.failureCategory)
  }
}
