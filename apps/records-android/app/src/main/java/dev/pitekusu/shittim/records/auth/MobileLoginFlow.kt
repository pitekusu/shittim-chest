package dev.pitekusu.shittim.records.auth

import android.os.SystemClock
import androidx.annotation.MainThread
import java.net.URI
import java.security.MessageDigest
import java.security.SecureRandom
import java.time.Clock
import java.time.Duration
import java.util.Base64

internal const val MOBILE_CALLBACK_URL = "$RECORDS_ORIGIN/auth/mobile/callback"

internal enum class MobileLoginStatus {
  SIGNED_IN, CANCELLED, REJECTED, EXPIRED, NETWORK, UNAVAILABLE,
  STORAGE_UNAVAILABLE, BROWSER_UNAVAILABLE,
}

internal class MobileLoginException(val status: MobileLoginStatus) :
  Exception("mobile_login_${status.name.lowercase()}")

/** One attempt in memory only. Call on Main; suspending HTTP does not block that thread. */
@MainThread
internal class MobileLoginFlow(
  private val client: MobileAuthClient,
  private val clock: Clock = Clock.systemUTC(),
  private val elapsedMillis: () -> Long = SystemClock::elapsedRealtime,
) {
  private var pending: Attempt? = null
  private val random = SecureRandom()

  suspend fun begin(returnTo: String): String {
    if (pending != null) throw MobileLoginException(MobileLoginStatus.REJECTED)
    val attempt = Attempt(randomValue(), randomValue(), returnTo, elapsedMillis() + 600_000)
    pending = attempt
    try {
      val response = client.start(MobileStartRequest(s256(attempt.verifier), attempt.state, returnTo))
      requireCurrent(attempt)
      val remaining = Duration.between(clock.instant(), response.expiresAt).toMillis()
      attempt.deadline = minOf(attempt.deadline, elapsedMillis() + remaining.coerceIn(0, 600_000))
      requireUnexpired(attempt)
      attempt.transaction = response.transactionId
      return RECORDS_ORIGIN + response.authorizePath
    } catch (error: Exception) {
      if (pending === attempt) pending = null
      throw error
    }
  }

  suspend fun complete(callbackUrl: String): MobileExchangeResponse {
    val attempt = pending ?: throw MobileLoginException(MobileLoginStatus.REJECTED)
    if (attempt.claimed || attempt.transaction == null) throw MobileLoginException(MobileLoginStatus.REJECTED)
    val callback = parseMobileCallback(callbackUrl)
    if (callback.transaction != attempt.transaction || !MessageDigest.isEqual(
        callback.state.toByteArray(Charsets.US_ASCII), attempt.state.toByteArray(Charsets.US_ASCII),
      )) throw MobileLoginException(MobileLoginStatus.REJECTED)
    // Claim before the first suspension; a second Intent cannot exchange the same code.
    attempt.claimed = true
    try {
      requireUnexpired(attempt)
      val response = client.exchange(MobileExchangeRequest(callback.transaction, callback.code, attempt.verifier))
      requireCurrent(attempt) // Cancellation during HTTP cannot later become a successful login.
      if (response.returnTo != attempt.returnTo || !response.expiresAt.isAfter(clock.instant())) {
        throw MobileLoginException(MobileLoginStatus.REJECTED)
      }
      return response
    } finally {
      if (pending === attempt) pending = null // Also discard an exchange with an unknown outcome.
    }
  }

  fun cancel() { pending = null }

  private fun requireCurrent(attempt: Attempt) {
    if (pending !== attempt) throw MobileLoginException(MobileLoginStatus.CANCELLED)
  }

  private fun requireUnexpired(attempt: Attempt) {
    if (elapsedMillis() >= attempt.deadline) throw MobileLoginException(MobileLoginStatus.EXPIRED)
  }

  private fun randomValue(): String = Base64.getUrlEncoder().withoutPadding()
    .encodeToString(ByteArray(32).also(random::nextBytes))

  private class Attempt(
    val verifier: String, val state: String, val returnTo: String, var deadline: Long,
    var transaction: String? = null, var claimed: Boolean = false,
  )
}

internal fun s256(verifier: String): String = Base64.getUrlEncoder().withoutPadding()
  .encodeToString(MessageDigest.getInstance("SHA-256").digest(verifier.toByteArray(Charsets.US_ASCII)))

// No data-class rendering: callback values must not leak through toString or exceptions.
internal class MobileCallback(val transaction: String, val code: String, val state: String)

internal fun parseMobileCallback(value: String): MobileCallback {
  try {
    require(value.length <= 512)
    val uri = URI(value)
    require(uri.scheme == "https" && uri.rawAuthority == "shittim.pitekusu.dev" &&
      uri.rawPath == "/auth/mobile/callback" && uri.rawFragment == null)
    val fields = requireNotNull(uri.rawQuery).split('&').map {
      // The server only emits ASCII base64url. Reject escapes, duplicates and extra fields.
      val pair = it.split('=')
      require(pair.size == 2 && mobileOpaqueValue.matches(pair[1]))
      pair[0] to pair[1]
    }
    require(fields.size == 3 && fields.map { it.first }.toSet() == setOf("transaction", "code", "state"))
    val query = fields.toMap()
    return MobileCallback(query.getValue("transaction"), query.getValue("code"), query.getValue("state"))
  } catch (_: Exception) {
    throw MobileLoginException(MobileLoginStatus.REJECTED)
  }
}
