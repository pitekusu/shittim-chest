package dev.pitekusu.shittim.records.auth

import java.time.Instant

/** Only the credential and its server-issued expiry; never a saved UI state or a log value. */
internal class StoredToken(val accessToken: String, val expiresAt: Instant) {
  init {
    require(
      accessToken.matches(Regex("[A-Za-z0-9_-]{43}")) &&
        expiresAt > Instant.EPOCH && expiresAt.nano == 0
    ) { "invalid_stored_token" }
  }

  // Deliberately not a data class: its generated toString would expose the bearer token.
  override fun toString(): String = "StoredToken(<redacted>)"
}

/** Do not attach platform exceptions: their messages are outside our privacy contract. */
internal class TokenStorageException : Exception("token_storage_unavailable")
