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
import kotlinx.coroutines.yield
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MobileSessionModelTest {
  @Test
  fun denialAfterSessionResponseCannotPublishItsOlderPermit() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        val model = fixture.start()
        model.await<SessionState.SignedIn>()
        yield()
        fixture.activationGate = CompletableDeferred()
        fixture.activationStarted = CompletableDeferred()
        model.onForeground()
        withTimeout(5_000) { fixture.activationStarted.await() }
        model.onAuthenticationRequired()
        assertNull(model.cachePermit.value)
        fixture.status = HttpStatusCode.Forbidden
        fixture.activationGate!!.complete(Unit)
        model.await<SessionState.SignedOut>()
        assertNull(model.cachePermit.value)
        assertNull(fixture.stored)
        assertEquals(3, fixture.gets) // The final check starts after the denial.
      }
    }
  }

  @Test
  fun logoutDuringRestoredAccessCancelsVerificationAndCompletesLocalDeletion() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = StoredToken(fixture.validToken.accessToken, fixture.validToken.expiresAt,
          CacheAuthorization("u".repeat(43), fixture.now, fixture.validToken.expiresAt))
        fixture.sessionGate = CompletableDeferred()
        val model = fixture.start()
        withTimeout(5_000) { fixture.sessionStarted.await() }
        model.logout()
        model.logout()
        assertEquals(SessionState.SigningOut, model.state.value)
        assertNull(model.offlineCacheAccountId)
        model.await<SessionState.SignedOut>()
        fixture.sessionGate!!.complete(Unit)
        yield()
        assertNull(fixture.stored)
        assertFalse(fixture.logoutPending)
        assertEquals(1, fixture.cacheClears)
        assertEquals(1, fixture.posts)
        assertTrue(fixture.activatedAccounts.isEmpty())
      }
    }
  }

  @Test
  fun savedPermitAllowsReadsBeforeSessionResponseButNeverAuthorizesNetworkRequests() = runBlocking {
    withContext(Dispatchers.Main) {
      for (hasPermit in listOf(true, false)) {
        Fixture().use { fixture ->
          val permit = CacheAuthorization("u".repeat(43), fixture.now, fixture.validToken.expiresAt)
          fixture.stored = StoredToken(fixture.validToken.accessToken, fixture.validToken.expiresAt,
            permit.takeIf { hasPermit })
          fixture.sessionGate = CompletableDeferred()
          fixture.status = HttpStatusCode.Unauthorized
          val model = fixture.start()
          withTimeout(5_000) { fixture.sessionStarted.await() }
          assertEquals(SessionState.Checking, model.state.value)
          assertEquals(hasPermit, model.isCacheAuthorized(permit.accountId))
          assertNull(model.withAuthorizedToken { fail("checking must not authorize a record request") })
          assertEquals(0, fixture.activatedAccounts.size)
          fixture.sessionGate!!.complete(Unit)
          model.await<SessionState.SignedOut>()
          assertNull(model.cachePermit.value)
          assertNull(model.offlineCacheAccountId)
        }
      }
    }
  }

  @Test
  fun rejectionDuringForegroundCheckImmediatelyLocksCacheAndNetworkFailureCannotRestoreIt() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        val model = fixture.start()
        model.await<SessionState.SignedIn>()
        yield()
        fixture.sessionStarted = CompletableDeferred()
        fixture.sessionGate = CompletableDeferred()
        fixture.status = HttpStatusCode.ServiceUnavailable
        model.onForeground()
        assertTrue(model.isCacheAuthorized("u".repeat(43)))
        withTimeout(5_000) { fixture.sessionStarted.await() }
        model.onAuthenticationRequired()
        assertNull(model.cachePermit.value)
        fixture.sessionGate!!.complete(Unit)
        model.await<SessionState.Unavailable>()
        assertNull(model.offlineCacheAccountId)
        fixture.sessionGate = null
        fixture.status = HttpStatusCode.OK
        yield()
        model.retry()
        model.await<SessionState.SignedIn>()
        assertTrue(model.isCacheAuthorized("u".repeat(43)))
      }
    }
  }

  @Test
  fun savedDeadlineExpiresEvenWhileSessionVerificationIsPending() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        val deadline = fixture.now.plusSeconds(1)
        fixture.stored = StoredToken("t".repeat(43), deadline,
          CacheAuthorization("u".repeat(43), fixture.now, deadline))
        fixture.sessionGate = CompletableDeferred()
        val model = fixture.start()
        withTimeout(5_000) { fixture.sessionStarted.await() }
        assertTrue(model.isCacheAuthorized("u".repeat(43)))
        model.await<SessionState.SignedOut>()
        fixture.sessionGate!!.complete(Unit)
        yield()
        assertNull(model.cachePermit.value)
        assertNull(fixture.stored)
        assertEquals(0, fixture.activatedAccounts.size)
      }
    }
  }

  @Test
  fun syncFailureOnlyLocksThePermitThatTheWorkerActuallyInvalidated() = runBlocking {
    withContext(Dispatchers.Main) {
      for (invalidated in listOf(false, true)) {
        Fixture().use { fixture ->
          fixture.stored = fixture.validToken
          val model = fixture.start()
          model.await<SessionState.SignedIn>()
          yield()
          fixture.status = HttpStatusCode.ServiceUnavailable
          if (invalidated) {
            fixture.sessionStarted = CompletableDeferred()
            fixture.sessionGate = CompletableDeferred()
            model.onForeground()
            withTimeout(5_000) { fixture.sessionStarted.await() }
            fixture.stored = fixture.validToken // Worker strips only its matching permit.
          }
          model.onSyncAuthenticationRequired()
          if (invalidated) {
            withTimeout(5_000) { model.cachePermit.first { it == null } }
            fixture.sessionGate!!.complete(Unit)
          }
          model.await<SessionState.Unavailable>()
          assertEquals(!invalidated, model.isCacheAuthorized("u".repeat(43)))
        }
      }
    }
  }

  @Test
  fun recordLinkBecomesLoginDestinationAndUpdatesActiveSession() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        val model = fixture.start()
        model.await<SessionState.SignedOut>()
        val first = "/records/${"a".repeat(43)}"
        val second = "/records/${"b".repeat(43)}"
        model.openDestination(first)
        assertEquals(first, model.loginDestination)
        assertTrue(model.beginLogin())
        model.openDestination(second) // A link cannot change an in-flight authorization request.
        assertEquals(first, model.loginDestination)
        fixture.stored = fixture.validToken
        model.loginResult(MobileLoginStep.Finished(MobileLoginStatus.SIGNED_IN, first))
        assertEquals(first, model.await<SessionState.SignedIn>().returnTo)
        model.openDestination(second)
        assertEquals(second, model.await<SessionState.SignedIn>().returnTo)
        model.openDestination("/admin")
        assertEquals(second, model.loginDestination)
        yield()
        model.onForeground()
        assertEquals(second, model.await<SessionState.SignedIn>().returnTo)
        model.closeDestination()
        assertEquals("/", model.await<SessionState.SignedIn>().returnTo)
        yield()
        model.onForeground()
        assertEquals("/", model.await<SessionState.SignedIn>().returnTo)
      }
    }
  }

  @Test
  fun expiredStoredSessionKeepsARecordLinkForTheNextLogin() = runBlocking {
    withContext(Dispatchers.Main) {
      for (serverRejects in listOf(false, true)) {
        Fixture().use { fixture ->
          fixture.stored = if (serverRejects) fixture.validToken
            else StoredToken("t".repeat(43), fixture.now)
          if (serverRejects) fixture.status = HttpStatusCode.Unauthorized
          val model = fixture.start()
          val destination = "/records/${"c".repeat(43)}"
          model.openDestination(destination)
          assertEquals(SessionNotice.EXPIRED, model.await<SessionState.SignedOut>().notice)
          assertEquals(destination, model.loginDestination)
        }
      }
    }
  }

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
        assertTrue(model.isCacheAuthorized("u".repeat(43)))
        fixture.sessionGate?.complete(Unit)
        model.await<SessionState.Unavailable>()
        assertNotNull(fixture.stored)
        val destination = "/records/${"r".repeat(43)}"
        model.openDestination(destination)
        assertEquals(destination, model.destination.value)
        assertNotNull(model.offlineCacheAccountId)
        model.closeDestination()
        assertEquals("/", model.destination.value)
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
        assertEquals(1, fixture.cacheClears)
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
      for (rejected in listOf(false, true)) {
        Fixture().use { fixture ->
          fixture.stored = fixture.validToken
          val model = fixture.start()
          model.await<SessionState.SignedIn>()
          val entered = CompletableDeferred<Unit>()
          val finish = CompletableDeferred<String>()
          val pending = async {
            model.withAuthorizedToken {
              entered.complete(Unit)
              val result = finish.await()
              if (rejected) throw MobileAuthException(MobileAuthFailure.AUTHENTICATION_REQUIRED)
              result
            }
          }
          entered.await()
          model.logout()
          model.await<SessionState.SignedOut>()
          yield()
          assertTrue(model.beginLogin())
          fixture.stored = StoredToken("x".repeat(43), fixture.validToken.expiresAt)
          model.loginResult(MobileLoginStep.Finished(MobileLoginStatus.SIGNED_IN))
          model.await<SessionState.SignedIn>()
          finish.complete("古い利用者の記録")
          assertNull(pending.await())
          assertTrue(model.isCacheAuthorized("u".repeat(43)))
        }
      }
    }
  }

  @Test
  fun offlinePermitSurvivesRestartButNotAnUnverifiedTokenOrExplicitDenial() = runBlocking {
    withContext(Dispatchers.Main) {
      val permit = CacheAuthorization("u".repeat(43), Instant.parse("2030-01-01T00:00:00Z"),
        Instant.parse("2030-01-01T00:10:00Z"))
      for (status in listOf(HttpStatusCode.ServiceUnavailable, HttpStatusCode.Forbidden,
        HttpStatusCode.Unauthorized, HttpStatusCode.Found)) {
        Fixture().use { fixture ->
          fixture.stored = StoredToken(fixture.validToken.accessToken, fixture.validToken.expiresAt, permit)
          fixture.status = status
          val model = fixture.start()
          if (status in listOf(HttpStatusCode.Unauthorized, HttpStatusCode.Forbidden)) {
            model.await<SessionState.SignedOut>()
          } else model.await<SessionState.Unavailable>()
          assertEquals(status == HttpStatusCode.ServiceUnavailable, model.isCacheAuthorized(permit.accountId))
          assertFalse(model.isCacheAuthorized("v".repeat(43)))
          assertEquals(0, fixture.cacheClears)
        }
      }
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken // Deployed v1 token has no verified account permit.
        fixture.status = HttpStatusCode.ServiceUnavailable
        val model = fixture.start()
        model.await<SessionState.Unavailable>()
        assertNull(model.offlineCacheAccountId)
      }
    }
  }

  @Test
  fun absoluteDeadlineCannotBeExtendedAndExpiryPreservesRecordsForReauthentication() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        fixture.serverExpiry = fixture.now.plusSeconds(90L * 86_400 + 600)
        val model = fixture.start()
        assertEquals(fixture.validToken.expiresAt, model.await<SessionState.SignedIn>().expiresAt)
        val saved = fixture.stored!!
        assertEquals(saved.expiresAt, saved.cacheAuthorization!!.expiresAt)
        assertEquals(listOf("u".repeat(43)), fixture.activatedAccounts)
      }
      Fixture().use { fixture ->
        val past = fixture.now.minusSeconds(90L * 86_400)
        fixture.stored = StoredToken("t".repeat(43), fixture.now,
          CacheAuthorization("u".repeat(43), past, fixture.now))
        val model = fixture.start()
        model.await<SessionState.SignedOut>()
        assertNull(model.offlineCacheAccountId)
        assertEquals(0, fixture.cacheClears)
        yield()
        assertTrue(model.beginLogin())
        fixture.stored = fixture.validToken
        model.loginResult(MobileLoginStep.Finished(MobileLoginStatus.SIGNED_IN))
        model.await<SessionState.SignedIn>()
        assertTrue(model.isCacheAuthorized("u".repeat(43)))
        assertEquals(0, fixture.cacheClears)
      }
    }
  }

  @Test
  fun failedCacheDeletionStillClearsCredentialsAndPreventsLoginUntilRetried() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        val model = fixture.start()
        model.await<SessionState.SignedIn>()
        fixture.cacheClearFails = true
        model.logout()
        model.await<SessionState.StorageError>()
        assertNull(fixture.stored)
        assertTrue(fixture.logoutPending)
        assertNull(model.offlineCacheAccountId)
        assertFalse(model.beginLogin())
        fixture.cacheClearFails = false
        yield()
        model.logout()
        model.await<SessionState.SignedOut>()
        assertEquals(2, fixture.cacheClears)
        assertEquals(1, fixture.posts)
        assertFalse(fixture.logoutPending)
      }
    }
  }

  @Test
  fun pendingLogoutAfterRestartNeverRestoresTheSessionAndCompletesCleanup() = runBlocking {
    withContext(Dispatchers.Main) {
      Fixture().use { fixture ->
        fixture.stored = fixture.validToken
        fixture.logoutPending = true
        val model = fixture.start()
        model.await<SessionState.SignedOut>()
        assertEquals(0, fixture.gets)
        assertEquals(1, fixture.cacheClears)
        assertEquals(1, fixture.posts)
        assertNull(fixture.stored)
        assertFalse(fixture.logoutPending)
        assertNull(model.offlineCacheAccountId)
      }
    }
  }

  @Test
  fun pendingLogoutWithoutReadableCredentialsResumesRecordDeletionBeforeAllowingLogin() = runBlocking {
    withContext(Dispatchers.Main) {
      for (readFails in listOf(false, true)) {
        Fixture().use { fixture ->
          // A prior logout erased credentials, but failed before the record deletion marker.
          fixture.logoutPending = true
          fixture.readFails = readFails
          fixture.cacheClearFails = true
          val model = fixture.start()
          model.await<SessionState.StorageError>()
          assertTrue(fixture.logoutPending)
          assertFalse(model.beginLogin())
          assertEquals(0, fixture.gets + fixture.posts)
          fixture.cacheClearFails = false
          yield()
          model.logout()
          model.await<SessionState.SignedOut>()
          assertFalse(fixture.logoutPending)
          assertEquals(2, fixture.cacheClears)
          assertTrue(model.beginLogin())
        }
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
    var cacheClearFails = false
    var logoutPending = false
    var cacheClears = 0
    val activatedAccounts = mutableListOf<String>()
    var gets = 0
    var posts = 0
    var status = HttpStatusCode.OK
    var sessionGate: CompletableDeferred<Unit>? = null
    var sessionStarted = CompletableDeferred<Unit>()
    var activationGate: CompletableDeferred<Unit>? = null
    var activationStarted = CompletableDeferred<Unit>()
    var logoutGate: CompletableDeferred<Unit>? = null
    val postStarted = CompletableDeferred<Unit>()
    val owner = ViewModelStore()
    val client = MobileAuthClient(MockEngine { request ->
      when (request.url.encodedPath.substringAfterLast('/')) {
        "session" -> {
          gets++
          sessionStarted.complete(Unit)
          sessionGate?.await()
          respond("""{"schemaVersion":1,"isAdmin":false,"cacheAccountId":"${"u".repeat(43)}","expiresAt":"$serverExpiry",
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
    }, { assertFalse(logoutPending); stored = it }, {
      assertNotEquals(Looper.getMainLooper(), Looper.myLooper())
      if (clearFails) throw TokenStorageException()
      stored = null
    }, {
      logoutPending = true
    }, { logoutPending }, {
      assertNull(stored)
      assertFalse(cacheClearFails)
      logoutPending = false
    }, { activationStarted.complete(Unit); activationGate?.await(); activatedAccounts.add(it) }, {
      cacheClears++
      if (cacheClearFails) throw dev.pitekusu.shittim.records.storage.RecordCacheException()
    }, Clock.fixed(now, ZoneOffset.UTC)).also { owner.put("session", it) }

    override fun close() { owner.clear(); client.close() }
  }
}
