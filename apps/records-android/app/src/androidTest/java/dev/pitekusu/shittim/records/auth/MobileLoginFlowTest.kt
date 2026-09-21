package dev.pitekusu.shittim.records.auth

import androidx.test.ext.junit.runners.AndroidJUnit4
import java.time.Clock
import java.time.ZoneOffset
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MobileLoginFlowTest {
  @Test
  fun independentPkceAndStateBindTheBrowserToOneExchangeAndOriginalDestination() = runBlocking {
    withContext(Dispatchers.Main) {
      // RFC 7636 Appendix B, a public test vector (not an application credential).
      assertEquals("E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"))
      MobileLoginFixture().use { fixture ->
        val flow = flow(fixture)
        val destination = "/records/${"r".repeat(43)}"
        assertEquals("$RECORDS_ORIGIN/api/v1/auth/mobile/authorize?transaction=${"t".repeat(42)}1",
          flow.begin(destination))
        val first = fixture.startBody
        assertEquals(setOf("codeChallenge", "codeChallengeMethod", "state", "returnTo"), first.keys)
        assertEquals("S256", first["codeChallengeMethod"])
        val callback = fixture.callback()
        val result = flow.complete(callback)
        assertEquals(destination, result.returnTo)
        assertEquals(fixture.token, result.accessToken)
        assertEquals(first["codeChallenge"], s256(fixture.exchangeBody.getValue("codeVerifier")))
        assertNotEquals(first["state"], fixture.exchangeBody["codeVerifier"])
        assertFalse(callback.contains(fixture.exchangeBody.getValue("codeVerifier")))
        rejected { flow.complete(callback) }
        assertEquals(1, fixture.exchanges)
        flow.begin("/")
        assertNotEquals(first["state"], fixture.startBody["state"])
        assertNotEquals(first["codeChallenge"], fixture.startBody["codeChallenge"])
      }
    }
  }

  @Test
  fun exactCallbackBoundaryAndBindingRejectUntrustedOrAmbiguousUrls() = runBlocking {
    withContext(Dispatchers.Main) {
      MobileLoginFixture().use { fixture ->
        val flow = flow(fixture)
        flow.begin("/")
        val callback = fixture.callback()
        val invalid = listOf(
          callback.replace("https:", "http:"), callback.replace("shittim.pitekusu.dev", "untrusted.invalid"),
          callback.replace("://", "://user@"),
          callback.replace("shittim.pitekusu.dev", "shittim.pitekusu.dev:443"),
          callback.replace("/callback?", "/callback/extra?"), callback + "#ignored",
          callback + "&extra=value", callback + "&code=${"c".repeat(43)}",
          callback.replace("state=", "code="), callback.replace("code=c", "code=%63"),
          callback.substringBefore("&state="), "x".repeat(513),
        )
        for (url in invalid) {
          val error = assertThrows(MobileLoginException::class.java) { parseMobileCallback(url) }
          assertEquals("mobile_login_rejected", error.message)
          assertNull(error.cause)
        }
        rejected { flow.complete(callback.replace("state=", "state=${"s".repeat(43)}&discard=")) }
        rejected { flow.complete(callback.replace(fixture.transaction, "z".repeat(43))) }
        rejected { flow.complete(callback.replace(fixture.startBody.getValue("state"), "s".repeat(43))) }
        assertEquals(0, fixture.exchanges)
        assertEquals(fixture.token, flow.complete(callback).accessToken)
      }
    }
  }

  @Test
  fun cancellationNewAttemptLostProcessAndDeadlineCannotReuseAnOldGrant() = runBlocking {
    withContext(Dispatchers.Main) {
      MobileLoginFixture().use { fixture ->
        val flow = flow(fixture)
        flow.begin("/")
        val old = fixture.callback()
        flow.cancel()
        rejected { flow.complete(old) }
        flow.begin("/")
        rejected { flow.complete(old) }
        rejected { flow(fixture).complete(fixture.callback()) }
        rejected { flow.begin("/") }
        fixture.elapsed = 600_000 // Exact monotonic deadline; wall-clock changes cannot extend it.
        rejected(MobileLoginStatus.EXPIRED) { flow.complete(fixture.callback()) }
        assertEquals(0, fixture.exchanges)
        fixture.elapsed = 0
        fixture.expiry = fixture.now.plusSeconds(30)
        flow.begin("/")
        fixture.elapsed = 30_000
        rejected(MobileLoginStatus.EXPIRED) { flow.complete(fixture.callback()) }
        assertEquals(0, fixture.exchanges)
      }
    }
  }

  @Test
  fun duplicateDuringExchangeAndUnknownResponseNeverReplayTheCode() = runBlocking {
    withContext(Dispatchers.Main) {
      MobileLoginFixture().use { fixture ->
        val flow = flow(fixture)
        flow.begin("/")
        val callback = fixture.callback()
        fixture.exchangeGate = CompletableDeferred()
        fixture.failExchange = true
        val attempt = async {
          try { flow.complete(callback); fail("expected network failure") }
          catch (error: MobileAuthException) { assertEquals(MobileAuthFailure.NETWORK, error.failure) }
        }
        withTimeout(5_000) { fixture.exchanging.await() }
        rejected { flow.complete(callback) }
        fixture.exchangeGate!!.complete(Unit)
        attempt.await()
        rejected { flow.complete(callback) }
        assertEquals(1, fixture.exchanges)
        assertTrue(fixture.exchangeBody.containsKey("codeVerifier"))
      }
    }
  }

  private fun flow(fixture: MobileLoginFixture) = MobileLoginFlow(
    fixture.client, Clock.fixed(fixture.now, ZoneOffset.UTC), { fixture.elapsed },
  )

  private suspend fun rejected(status: MobileLoginStatus = MobileLoginStatus.REJECTED, block: suspend () -> Any) {
    try { block(); fail("expected rejected handoff") }
    catch (error: MobileLoginException) { assertEquals(status, error.status); assertNull(error.cause) }
  }
}
