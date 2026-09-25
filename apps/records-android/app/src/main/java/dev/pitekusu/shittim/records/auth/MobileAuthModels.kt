package dev.pitekusu.shittim.records.auth

import java.net.URI
import java.time.Instant
import java.time.OffsetDateTime
import kotlinx.serialization.KSerializer
import kotlinx.serialization.Serializable
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder

internal val mobileOpaqueValue = Regex("[A-Za-z0-9_-]{43}")
private val returnDestination = Regex("/(?:records/[A-Za-z0-9_-]{43})?")

// Regular classes intentionally avoid data-class toString() exposing grants or user data.
@Serializable
internal class MobileStartRequest(
  val codeChallenge: String,
  val state: String,
  val returnTo: String = "/",
  val codeChallengeMethod: String = "S256",
) {
  init {
    require(mobileOpaqueValue.matches(codeChallenge) && mobileOpaqueValue.matches(state) &&
      returnDestination.matches(returnTo) && codeChallengeMethod == "S256") { "invalid_mobile_start" }
  }
}

@Serializable
internal class MobileExchangeRequest(
  val transactionId: String,
  val code: String,
  val codeVerifier: String,
) {
  init {
    require(mobileOpaqueValue.matches(transactionId) && mobileOpaqueValue.matches(code) &&
      Regex("[A-Za-z0-9._~-]{43,128}").matches(codeVerifier)) { "invalid_mobile_exchange" }
  }
}

@Serializable
internal class MobileStartResponse(
  val schemaVersion: Int,
  val transactionId: String,
  val authorizePath: String,
  @Serializable(with = MobileExpirySerializer::class) val expiresAt: Instant,
) {
  init {
    require(schemaVersion == 1 && mobileOpaqueValue.matches(transactionId) &&
      authorizePath == "/api/v1/auth/mobile/authorize?transaction=$transactionId") { "invalid_mobile_start" }
  }
}

@Serializable
internal class MobileSessionResponse(
  val schemaVersion: Int,
  val cacheAccountId: String,
  val user: MobileSessionUser,
  val isAdmin: Boolean,
  @Serializable(with = MobileExpirySerializer::class) val expiresAt: Instant,
) {
  init { require(schemaVersion == 1 && mobileOpaqueValue.matches(cacheAccountId)) { "invalid_mobile_session" } }
}

@Serializable
internal class MobileExchangeResponse(
  val schemaVersion: Int,
  val cacheAccountId: String,
  val accessToken: String,
  val tokenType: String,
  @Serializable(with = MobileExpirySerializer::class) val expiresAt: Instant,
  val user: MobileSessionUser,
  val isAdmin: Boolean,
  val returnTo: String,
) {
  init {
    require(schemaVersion == 1 && mobileOpaqueValue.matches(cacheAccountId) &&
      mobileOpaqueValue.matches(accessToken) && tokenType == "Bearer" &&
      returnDestination.matches(returnTo)) { "invalid_mobile_exchange" }
  }
}

@Serializable
internal class MobileSessionUser(val displayName: String, val avatar: MobileAvatar) {
  init { require(displayName.isNotBlank()) { "invalid_mobile_user" } }
}

@Serializable
internal class MobileAvatar(
  val kind: String,
  val alt: String,
  val fallbackVariant: String,
  val url: String? = null,
) {
  init {
    require(alt.isNotBlank() && fallbackVariant in setOf("cyan", "pink", "lavender")) { "invalid_mobile_avatar" }
    require(when (kind) {
      "placeholder" -> url == null
      "image" -> url != null && URI(url).let { it.scheme == "https" && it.host != null && it.userInfo == null }
      else -> false
    }) { "invalid_mobile_avatar" }
  }
}

internal object MobileExpirySerializer : KSerializer<Instant> {
  override val descriptor = PrimitiveSerialDescriptor("MobileExpiry", PrimitiveKind.STRING)
  override fun serialize(encoder: Encoder, value: Instant) = encoder.encodeString(value.toString())
  override fun deserialize(decoder: Decoder): Instant = OffsetDateTime.parse(decoder.decodeString()).toInstant().also {
    // The server issues epoch-second expiries; C13 stores that exact absolute deadline.
    require(it > Instant.EPOCH && it.nano == 0) { "invalid_mobile_expiry" }
  }
}
