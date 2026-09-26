package dev.pitekusu.shittim.records.auth

import java.time.Instant
import java.time.Duration

/** An offline permit is issued only after session verification, never from an Activity result. */
internal class CacheAuthorization(val accountId: String, val verifiedAt: Instant, val expiresAt: Instant) {
  init {
    require(mobileOpaqueValue.matches(accountId) && verifiedAt > Instant.EPOCH &&
      verifiedAt.nano == 0 && expiresAt.nano == 0 && expiresAt > verifiedAt &&
      expiresAt <= verifiedAt.plus(Duration.ofDays(90))) { "invalid_cache_authorization" }
  }

  fun permits(now: Instant): Boolean = now >= verifiedAt && now < expiresAt
}

/** Credentials and a verified cache permit only; no saved profile or UI state. */
internal class StoredToken(val accessToken: String, val expiresAt: Instant,
  val cacheAuthorization: CacheAuthorization? = null, val logoutPending: Boolean = false) {
  init {
    require(
      accessToken.matches(Regex("[A-Za-z0-9_-]{43}")) &&
        expiresAt > Instant.EPOCH && expiresAt.nano == 0 &&
        (cacheAuthorization == null || cacheAuthorization.expiresAt <= expiresAt)
    ) { "invalid_stored_token" }
  }

  // Deliberately not a data class: its generated toString would expose the bearer token.
  override fun toString(): String = "StoredToken(<redacted>)"
}

/** Do not attach platform exceptions: their messages are outside our privacy contract. */
internal class TokenStorageException : Exception("token_storage_unavailable")
