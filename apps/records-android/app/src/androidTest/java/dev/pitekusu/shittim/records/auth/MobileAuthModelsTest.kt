package dev.pitekusu.shittim.records.auth

import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.serialization.json.Json
import org.junit.Assert.assertThrows
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MobileAuthModelsTest {
  private val opaque = "t".repeat(43)

  @Test
  fun localRequestsRequireS256AndAnAllowedReturnDestination() {
    MobileStartRequest(opaque, opaque, "/records/$opaque")
    MobileExchangeRequest(opaque, opaque, "v".repeat(128))
    assertThrows(IllegalArgumentException::class.java) { MobileStartRequest(opaque, opaque, "//untrusted.invalid") }
    assertThrows(IllegalArgumentException::class.java) { MobileStartRequest(opaque, opaque, codeChallengeMethod = "plain") }
    assertThrows(IllegalArgumentException::class.java) { MobileExchangeRequest(opaque, opaque, "short") }
  }

  @Test
  fun browserDestinationMustMatchTheIssuedTransactionAndExpiryMustHaveAnOffset() {
    val body = """{"schemaVersion":1,"transactionId":"$opaque",
      "authorizePath":"/api/v1/auth/mobile/authorize?transaction=$opaque",
      "expiresAt":"2030-01-01T00:00:00Z"}"""
    for (invalid in listOf(
      body.replace("/api/v1/auth/mobile/authorize", "https://untrusted.invalid/authorize"),
      body.replace("?transaction=$opaque", "?transaction=${"x".repeat(43)}"),
      body.replace("2030-01-01T00:00:00Z", "2030-01-01T00:00:00"),
    )) {
      assertThrows(Exception::class.java) { Json.decodeFromString<MobileStartResponse>(invalid) }
    }
  }
}
