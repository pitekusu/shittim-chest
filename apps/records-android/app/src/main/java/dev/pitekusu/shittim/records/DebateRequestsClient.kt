package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.RECORDS_ORIGIN
import dev.pitekusu.shittim.records.auth.mobileOpaqueValue
import io.ktor.client.HttpClient
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.accept
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.header
import io.ktor.client.request.parameter
import io.ktor.client.request.prepareRequest
import io.ktor.client.request.setBody
import io.ktor.client.statement.bodyAsChannel
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.content.OutgoingContent
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import io.ktor.utils.io.ByteReadChannel
import io.ktor.utils.io.readBuffer
import java.io.Closeable
import java.io.IOException
import java.time.Instant
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.io.readByteArray
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.CookieJar

internal fun validDebateRequestId(value: String): Boolean =
  Regex("[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}").matches(value)

internal fun validDebateQuestion(value: String): Boolean = value.isNotBlank() &&
  value.codePointCount(0, value.length) <= 1000 &&
  value.codePoints().noneMatch { it in 0xD800..0xDFFF }

@Serializable
internal class DebateRequest(
  val requestId: String,
  val question: String,
  val status: String,
  val phase: String? = null,
  val createdAt: String,
  val updatedAt: String,
  val recordId: String? = null,
  val errorCode: String? = null,
) {
  val terminal: Boolean get() = status in setOf("published", "failed", "cancelled")
  fun validate() {
    check(validDebateRequestId(requestId) && validDebateQuestion(question))
    check(status in setOf("accepted", "queued", "starting", "running", "publishing", "published", "failed", "cancelled"))
    check(createdAt.endsWith("Z") && updatedAt.endsWith("Z"))
    check(Instant.parse(updatedAt) >= Instant.parse(createdAt))
    check(phase == null || phase.matches(Regex("[A-Za-z_]{1,64}")))
    check(recordId == null || mobileOpaqueValue.matches(recordId))
    check((status == "published") == (recordId != null))
    check(errorCode == null || errorCode.matches(Regex("[A-Z_]{1,64}")))
  }
  override fun toString(): String = "DebateRequest(<redacted>)"
}

@Serializable internal class DebateRequestPage(val items: List<DebateRequest>, val nextCursor: String? = null)
internal enum class DebateFailure { AUTH_REQUIRED, REAUTH_REQUIRED, FORBIDDEN, NOT_FOUND, CONFLICT, QUEUE_FULL, UNAVAILABLE, INVALID_RESPONSE, STORAGE }
internal class DebateRequestException(val failure: DebateFailure) : Exception("debate_request_${failure.name.lowercase()}")

/** Normal JSON uses Ktor; POST is one-shot so an ambiguous response cannot cause implicit replay. */
internal class DebateRequestsClient(private val engine: HttpClientEngine = OkHttp.create {
  config { retryOnConnectionFailure(false); followRedirects(false); followSslRedirects(false)
    cookieJar(CookieJar.NO_COOKIES); cache(null) }
}) : Closeable {
  private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
  private val client = HttpClient(engine) {
    expectSuccess = false
    followRedirects = false
    install(ContentNegotiation) { json(json) }
    install(HttpTimeout) { connectTimeoutMillis = 10_000; socketTimeoutMillis = 20_000; requestTimeoutMillis = 20_000 }
  }

  suspend fun submit(token: String, requestId: String, question: String): DebateRequest {
    check(validDebateRequestId(requestId) && validDebateQuestion(question))
    val result = call<DebateRequest>(token, HttpMethod.Post, "", json.encodeToString(Start(requestId, question)))
    if (result.requestId != requestId || result.question != question) throw DebateRequestException(DebateFailure.INVALID_RESPONSE)
    result.validate()
    return result
  }

  suspend fun find(token: String, requestId: String): DebateRequest? {
    check(validDebateRequestId(requestId))
    return try {
      call<DebateRequest>(token, HttpMethod.Get, "/$requestId").also {
        it.validate()
        check(it.requestId == requestId)
      }
    } catch (error: DebateRequestException) { if (error.failure == DebateFailure.NOT_FOUND) null else throw error }
  }

  suspend fun list(token: String, cursor: String? = null): DebateRequestPage {
    check(cursor == null || cursor.length in 1..4096)
    return call<DebateRequestPage>(token, HttpMethod.Get, "", cursor = cursor).also { page ->
      check(page.items.size <= 50 && page.items.map { it.requestId }.distinct().size == page.items.size)
      page.items.forEach(DebateRequest::validate)
      check(page.nextCursor == null || (page.nextCursor.length in 1..4096 && page.nextCursor != cursor))
    }
  }

  private suspend inline fun <reified T> call(token: String, method: HttpMethod, path: String,
    body: String? = null, cursor: String? = null): T {
    try {
      check(mobileOpaqueValue.matches(token))
      return client.prepareRequest("$RECORDS_ORIGIN/api/v1/debate-requests$path") {
        this.method = method
        bearerAuth(token)
        accept(ContentType.Application.Json)
        header(HttpHeaders.CacheControl, "no-store")
        if (cursor != null) parameter("cursor", cursor)
        if (body != null) {
          val bytes = body.encodeToByteArray()
          setBody(object : OutgoingContent.ReadChannelContent() {
            override val contentType = ContentType.Application.Json
            override val contentLength = bytes.size.toLong()
            override fun readFrom() = ByteReadChannel(bytes)
          })
        }
      }.execute { response ->
        check(response.contentType()?.withoutParameters() == ContentType.Application.Json)
        val bytes = response.bodyAsChannel().readBuffer(262_145).readByteArray()
        check(bytes.size <= 262_144)
        val text = bytes.decodeToString(throwOnInvalidSequence = true)
        if (response.status.value !in setOf(200, 201, 202)) {
          val code = json.decodeFromString<ErrorEnvelope>(text).error.code
          throw DebateRequestException(when (response.status.value) {
            401 -> DebateFailure.AUTH_REQUIRED
            403 -> when (code) {
              "GUILD_MEMBERSHIP_REQUIRED" -> DebateFailure.AUTH_REQUIRED
              "DEBATE_START_REAUTH_REQUIRED" -> DebateFailure.REAUTH_REQUIRED
              else -> DebateFailure.FORBIDDEN
            }
            404 -> DebateFailure.NOT_FOUND
            409 -> DebateFailure.CONFLICT
            429 -> if (code == "DEBATE_QUEUE_FULL") DebateFailure.QUEUE_FULL else DebateFailure.UNAVAILABLE
            else -> DebateFailure.UNAVAILABLE
          })
        }
        json.decodeFromString<T>(text)
      }
    } catch (error: CancellationException) { throw error }
    catch (error: DebateRequestException) { throw error }
    catch (_: IOException) { throw DebateRequestException(DebateFailure.UNAVAILABLE) }
    catch (_: Exception) { throw DebateRequestException(DebateFailure.INVALID_RESPONSE) }
  }

  override fun close() { client.close(); engine.close() }
  @Serializable private class Start(val requestId: String, val question: String)
  @Serializable private class ErrorEnvelope(val error: ErrorBody)
  @Serializable private class ErrorBody(val code: String)
}
