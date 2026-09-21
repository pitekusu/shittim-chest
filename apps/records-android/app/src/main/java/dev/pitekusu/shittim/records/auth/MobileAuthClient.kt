package dev.pitekusu.shittim.records.auth

import io.ktor.client.HttpClient
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.request.accept
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.header
import io.ktor.client.request.prepareRequest
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsChannel
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.content.OutgoingContent
import io.ktor.http.contentType
import io.ktor.utils.io.ByteReadChannel
import io.ktor.utils.io.readBuffer
import java.io.Closeable
import java.io.IOException
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.io.readByteArray
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import okhttp3.CookieJar

internal const val RECORDS_ORIGIN = "https://shittim.pitekusu.dev"

internal enum class MobileAuthFailure {
  NETWORK, REQUEST_REJECTED, GRANT_REJECTED, AUTHENTICATION_REQUIRED,
  FORBIDDEN, THROTTLED, UNAVAILABLE, INVALID_RESPONSE,
}

/** Fixed categories only: never chain HTTP, JSON, or TLS exceptions containing private inputs. */
internal class MobileAuthException(val failure: MobileAuthFailure) :
  Exception("mobile_auth_${failure.name.lowercase()}")

/** Owns its engine; reuse until the caller's scope ends. No UI, storage, or automatic re-login. */
internal class MobileAuthClient(private val engine: HttpClientEngine = OkHttp.create {
  config {
    retryOnConnectionFailure(false) // A one-time exchange must not be replayed implicitly.
    followRedirects(false)
    followSslRedirects(false)
    cookieJar(CookieJar.NO_COOKIES)
    cache(null)
  }
}) : Closeable {
  private val json = Json { encodeDefaults = true; ignoreUnknownKeys = true }
  private val client = HttpClient(engine) {
    expectSuccess = false // Classify status without an exception carrying response content.
    followRedirects = false
    install(HttpTimeout) {
      connectTimeoutMillis = 10_000
      socketTimeoutMillis = 20_000
      requestTimeoutMillis = 20_000
    }
    // No Auth, Cookies, Cache, Logging, or Retry plugins on the native login transport.
  }

  suspend fun start(request: MobileStartRequest): MobileStartResponse =
    call("start", HttpMethod.Post, body = json.encodeToString(request)) { json.decodeFromString(it) }

  suspend fun exchange(request: MobileExchangeRequest): MobileExchangeResponse =
    call("exchange", HttpMethod.Post, body = json.encodeToString(request)) { json.decodeFromString(it) }

  suspend fun session(accessToken: String): MobileSessionResponse =
    call("session", HttpMethod.Get, accessToken = accessToken) { json.decodeFromString(it) }

  suspend fun logout(accessToken: String): Unit =
    call("logout", HttpMethod.Post, accessToken = accessToken, expectedStatus = 204) { }

  private suspend fun <T> call(
    route: String,
    method: HttpMethod,
    body: String? = null,
    accessToken: String? = null,
    expectedStatus: Int = 200,
    decode: (String) -> T,
  ): T {
    if (accessToken != null && !mobileOpaqueValue.matches(accessToken)) {
      throw MobileAuthException(MobileAuthFailure.REQUEST_REJECTED)
    }
    try {
      return client.prepareRequest("$RECORDS_ORIGIN/api/v1/auth/mobile/$route") {
        this.method = method
        accept(ContentType.Application.Json)
        header(HttpHeaders.CacheControl, "no-store")
        if (accessToken != null) bearerAuth(accessToken)
        if (method == HttpMethod.Post) {
          val payload = body?.encodeToByteArray() ?: ByteArray(0)
          // Ktor maps channel content to OkHttp's one-shot body, including empty logout.
          // retryOnConnectionFailure alone does not prevent a 503 Retry-After: 0 replay.
          setBody(object : OutgoingContent.ReadChannelContent() {
            override val contentType = body?.let { ContentType.Application.Json }
            override val contentLength = payload.size.toLong()
            override fun readFrom() = ByteReadChannel(payload)
          })
        }
      }.execute { response ->
        // Never retain or echo error pages; only 400 needs its allowlisted grant category.
        val failure = when (response.status.value) {
          expectedStatus, 400 -> null
          401 -> MobileAuthFailure.AUTHENTICATION_REQUIRED
          403 -> MobileAuthFailure.FORBIDDEN
          429 -> MobileAuthFailure.THROTTLED
          in 500..599 -> MobileAuthFailure.UNAVAILABLE
          else -> MobileAuthFailure.INVALID_RESPONSE // Includes redirects; do not follow them.
        }
        if (failure != null) throw MobileAuthException(failure)
        if (response.status.value == 204) return@execute decode("")
        check(response.contentType()?.withoutParameters() == ContentType.Application.Json)
        // Scoped streaming caps allocation even when Content-Length is missing or false.
        val bytes = response.bodyAsChannel().readBuffer(65_537L).readByteArray()
        check(bytes.size <= 65_536)
        val text = bytes.decodeToString(throwOnInvalidSequence = true)
        if (response.status.value == 400) {
          val error = json.decodeFromString<ErrorEnvelope>(text)
          throw MobileAuthException(if (error.error.code == "MOBILE_GRANT_INVALID")
            MobileAuthFailure.GRANT_REJECTED else MobileAuthFailure.REQUEST_REJECTED)
        }
        decode(text)
      }
    } catch (error: CancellationException) {
      throw error // Caller cancellation is neither an expired session nor a network failure.
    } catch (error: MobileAuthException) {
      throw error
    } catch (_: IOException) {
      throw MobileAuthException(MobileAuthFailure.NETWORK)
    } catch (_: Exception) {
      throw MobileAuthException(MobileAuthFailure.INVALID_RESPONSE)
    }
  }

  override fun close() {
    client.close()
    engine.close()
  }

  @Serializable private class ErrorEnvelope(val error: ErrorCode)
  @Serializable private class ErrorCode(val code: String)
}
