package dev.pitekusu.shittim.records.auth

import android.os.Looper
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MobileSessionModelTest {
  @Test
  fun loginIsSingleFlightAndReadsSavedCredentialsInsteadOfTrustingResult() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        val model = fixture.start()
        model.await<SessionState.SignedOut>()
        assertTrue(model.beginLogin())
        assertFalse(model.beginLogin())
        model.loginResult(MobileLoginStep.Finished(MobileLoginStatus.CANCELLED))
        assertEquals(SessionNotice.CANCELLED, model.await<SessionState.SignedOut>().notice)
        assertTrue(model.beginLogin())
        fixture.stored = fixture.validToken
        val destination = "/records/${"r".repeat(43)}"
        val result = MobileLoginStep.Finished(MobileLoginStatus.SIGNED_IN, destination)
        model.loginResult(result)
        model.loginResult(result)
        val state = model.await<SessionState.SignedIn>()
        assertEquals(destination, state.returnTo)
        assertEquals("利用者A", state.user.displayName)
        assertEquals(1, fixture.gets)
        assertFalse(state.toString().contains(fixture.validToken.accessToken))
      }
    }
  }

  @Test
  fun expiredStorageAndServerRevocationClearAccessWithoutTreatingNetworkFailureAsExpiry() = runBlocking {
    withContext(Dispatchers.Main) {
      for (localExpired in listOf(true, false)) {
        Fixture().use { fixture ->
          fixture.stored = if (localExpired) StoredToken("t".repeat(43), fixture.now) else fixture.validToken
          fixture.status = HttpStatusCode.Unauthorized
          val model = fixture.start()
          assertEquals(SessionNotice.EXPIRED, model.await<SessionState.SignedOut>().notice)
          assertNull(fixture.stored)
          assertEquals(if (localExpired) 0 else 1, fixture.gets)
        }
      }
    }
  }

  @Test
  fun failedSessionCheckHidesIdentityButKeepsCredentialsForExplicitRetry() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        val model = fixture.start()
        model.await<SessionState.SignedIn>()
        fixture.status = HttpStatusCode.ServiceUnavailable
        fixture.sessionGate = CompletableDeferred()
        model.onForeground()
        assertEquals(SessionState.Checking, model.state.value)
        fixture.sessionGate?.complete(Unit)
        model.await<SessionState.Unavailable>()
        assertNotNull(fixture.stored)
        fixture.status = HttpStatusCode.OK
        fixture.sessionGate = null
        model.retry()
        assertEquals("利用者A", model.await<SessionState.SignedIn>().user.displayName)
      }
    }
  }

  @Test
  fun logoutImmediatelyHidesUserAndNeverRetriesAnUncertainPost() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        val model = fixture.start()
        model.await<SessionState.SignedIn>()
        fixture.logoutGate = CompletableDeferred()
        fixture.status = HttpStatusCode.ServiceUnavailable
        model.logout()
        model.logout()
        assertEquals(SessionState.SigningOut, model.state.value)
        fixture.postStarted.await()
        assertNull(fixture.stored)
        fixture.logoutGate!!.complete(Unit)
        assertEquals(SessionNotice.LOCAL_LOGOUT, model.await<SessionState.SignedOut>().notice)
        assertEquals(1, fixture.posts)
        // Account switching shows only the newly verified profile.
        assertTrue(model.beginLogin())
        fixture.stored = fixture.validToken
        fixture.name = "利用者B"
        fixture.status = HttpStatusCode.OK
        model.loginResult(MobileLoginStep.Finished(MobileLoginStatus.SIGNED_IN))
        assertEquals("利用者B", model.await<SessionState.SignedIn>().user.displayName)
      }
    }
  }

  @Test
  fun storageFailureIsClosedUntilExplicitDeletionSucceeds() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.readFails = true
        fixture.clearFails = true
        val model = fixture.start()
        model.await<SessionState.StorageError>()
        assertFalse(model.beginLogin())
        model.logout()
        model.await<SessionState.StorageError>()
        fixture.clearFails = false
        model.logout()
        model.await<SessionState.SignedOut>()
        assertEquals(0, fixture.gets + fixture.posts)
      }
    }
  }

  @Test
  fun serverDeadlineLocksVisibleScreenWithoutAnotherNetworkRequest() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        fixture.serverExpiry = fixture.now.plusSeconds(1)
        val model = fixture.start()
        assertEquals(fixture.serverExpiry, model.await<SessionState.SignedIn>().expiresAt)
        assertEquals(SessionNotice.EXPIRED, model.await<SessionState.SignedOut>().notice)
        assertNull(fixture.stored)
        assertEquals(1, fixture.gets)
      }
    }
  }

  @Test
  fun responseFinishingAfterLogoutCannotBeUsedForTheOldAccount() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        val model = fixture.start()
        model.await<SessionState.SignedIn>()
        val entered = CompletableDeferred<Unit>()
        val finish = CompletableDeferred<String>()
        val pending = async {
          model.withAuthorizedToken { entered.complete(Unit); finish.await() }
        }
        entered.await()
        model.logout()
        model.await<SessionState.SignedOut>()
        finish.complete("古い利用者の記録")
        assertNull(pending.await())
      }
    }
  }

  private suspend inline fun <reified T : SessionState> MobileSessionModel.await(): T =
    withTimeout(5_000) { state.first { it is T } as T }

  private class Fixture : AutoCloseable {
    val now = Instant.parse("2030-01-01T00:00:00Z")
    val validToken = StoredToken("t".repeat(43), now.plusSeconds(600))
    var stored: StoredToken? = null
    var serverExpiry = validToken.expiresAt
    var name = "利用者A"
    var readFails = false
    var clearFails = false
    var gets = 0
    var posts = 0
    var status = HttpStatusCode.OK
    var sessionGate: CompletableDeferred<Unit>? = null
    var logoutGate: CompletableDeferred<Unit>? = null
    val postStarted = CompletableDeferred<Unit>()
    val owner = ViewModelStore()
    val client = MobileAuthClient(MockEngine { request ->
      when (request.url.encodedPath.substringAfterLast('/')) {
        "session" -> {
          gets++
          sessionGate?.await()
          respond("""{"schemaVersion":1,"isAdmin":false,"expiresAt":"$serverExpiry",
            "user":{"displayName":"$name","avatar":{"kind":"placeholder","alt":"架空","fallbackVariant":"cyan"}}}""",
            status, headersOf(HttpHeaders.ContentType, "application/json"))
        }
        "logout" -> {
          posts++
          postStarted.complete(Unit)
          logoutGate?.await()
          respond("", if (status == HttpStatusCode.OK) HttpStatusCode.NoContent else status)
        }
        else -> error("unexpected_test_request")
      }
    })

    fun start(): MobileSessionModel = MobileSessionModel(client, {
      assertNotEquals(Looper.getMainLooper(), Looper.myLooper())
      if (readFails) throw TokenStorageException()
      stored
    }, {
      assertNotEquals(Looper.getMainLooper(), Looper.myLooper())
      if (clearFails) throw TokenStorageException()
      stored = null
    }, Clock.fixed(now, ZoneOffset.UTC)).also { owner.put("session", it) }

    override fun close() { owner.clear(); client.close() }
  }
}
