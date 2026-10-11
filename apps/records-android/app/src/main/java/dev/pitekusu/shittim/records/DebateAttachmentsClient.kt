package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.RECORDS_ORIGIN
import dev.pitekusu.shittim.records.auth.mobileOpaqueValue
import io.ktor.client.HttpClient
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.header
import io.ktor.client.request.prepareRequest
import io.ktor.client.request.setBody
import io.ktor.client.request.forms.MultiPartFormDataContent
import io.ktor.client.request.forms.formData
import io.ktor.client.statement.bodyAsChannel
import io.ktor.http.ContentType
import io.ktor.http.Headers
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import io.ktor.utils.io.readBuffer
import java.io.Closeable
import java.time.Instant
import java.util.Base64
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.io.readByteArray
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.CookieJar

@Serializable internal class AttachmentUploadStatus(val schemaVersion: Int, val attachmentId: String,
  val requestId: String, val status: String, val image: RecordAttachment? = null) {
  fun validate(request: String, draft: DraftAttachment) {
    check(schemaVersion == 1 && attachmentId == draft.attachmentId && requestId == request)
    check(status in setOf("reserved", "promoting", "ready", "consumed", "expired"))
    check(status !in setOf("ready", "consumed") || image != null)
    image?.let { it.validate(); check(it.sameImage(draft.descriptor())) }
  }
}
@Serializable internal class AttachmentUploadTicket(val schemaVersion: Int, val attachmentId: String,
  val method: String, val uploadUrl: String, val expiresAt: String, val fields: Map<String, String?>) {
  fun validate(draft: DraftAttachment) {
    check(schemaVersion == 1 && attachmentId == draft.attachmentId && method == "POST" &&
      validAttachmentS3Url(uploadUrl, upload = true) && Instant.parse(expiresAt) > Instant.now())
    check(fields["Content-Type"] == draft.contentType && fields["key"]?.matches(Regex("debate-uploads/[A-Za-z0-9_-]+\\.bin")) == true)
    val digest = draft.sha256.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
    check(fields["x-amz-checksum-sha256"] == Base64.getEncoder().encodeToString(digest))
    check(fields.keys.all { it in setOf("key", "Content-Type", "x-amz-checksum-sha256", "x-amz-algorithm",
      "x-amz-credential", "x-amz-date", "x-amz-security-token", "policy", "x-amz-signature") })
    check(fields["x-amz-algorithm"] == "AWS4-HMAC-SHA256" &&
      listOf("x-amz-credential", "x-amz-date", "policy", "x-amz-signature").all { !fields[it].isNullOrBlank() })
  }
}

/** Uploads only immutable encrypted draft bytes. S3 receives neither the app token nor cookies. */
internal class DebateAttachmentsClient(private val engine: HttpClientEngine = OkHttp.create {
  config { retryOnConnectionFailure(false); followRedirects(false); followSslRedirects(false)
    cookieJar(CookieJar.NO_COOKIES); cache(null) }
}) : Closeable {
  private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
  private val client = HttpClient(engine) {
    expectSuccess = false; followRedirects = false
    install(ContentNegotiation) { json(json) }
    install(HttpTimeout) { connectTimeoutMillis = 10_000; socketTimeoutMillis = 30_000; requestTimeoutMillis = 90_000 }
  }

  suspend fun prepare(token: String, requestId: String, images: List<DraftAttachment>,
    load: suspend (DraftAttachment) -> ByteArray) = try {
    check(images.size <= MAX_ATTACHMENT_COUNT && images.map { it.attachmentId }.distinct().size == images.size)
    for (draft in images) {
      draft.validate()
      val path = "/${draft.attachmentId}"
      val previous = try { call<AttachmentUploadStatus>(token, HttpMethod.Get, path).also { it.validate(requestId, draft) } }
      catch (error: DebateRequestException) { if (error.failure == DebateFailure.NOT_FOUND) null else throw error }
      if (previous?.status in setOf("ready", "consumed")) continue
      if (previous?.status == "expired") throw DebateRequestException(DebateFailure.ATTACHMENT_EXPIRED)
      if (previous != null) {
        try {
          val completed = call<AttachmentUploadStatus>(token, HttpMethod.Post, "$path/complete")
          completed.validate(requestId, draft)
          if (completed.status in setOf("ready", "consumed")) continue
          throw DebateRequestException(DebateFailure.UNAVAILABLE)
        } catch (error: DebateRequestException) {
          if (error.failure != DebateFailure.ATTACHMENT_NOT_READY) throw error
          // An active promotion is reconciled later; never replace its upload source.
          if (previous.status == "promoting") throw DebateRequestException(DebateFailure.UNAVAILABLE)
        }
      }
      val ticket = call<AttachmentUploadTicket>(token, HttpMethod.Post, "",
        json.encodeToString(UploadRequest(requestId, draft.attachmentId, draft.contentType, draft.sizeBytes, draft.sha256)))
      ticket.validate(draft)
      val bytes = load(draft)
      try {
        validateAttachmentBytes(bytes, draft.descriptor())
        val body = MultiPartFormDataContent(formData {
          ticket.fields.forEach { (name, value) -> if (value != null) append(name, value) }
          append("file", bytes, Headers.build {
            append(HttpHeaders.ContentType, draft.contentType)
            append(HttpHeaders.ContentDisposition, "filename=attachment")
          })
        })
        client.prepareRequest(ticket.uploadUrl) {
          method = HttpMethod.Post
          setBody(body)
        }.execute { if (it.status.value != 204) throw DebateRequestException(DebateFailure.UNAVAILABLE) }
      } catch (error: CancellationException) { throw error }
      catch (error: DebateRequestException) { throw error }
      catch (_: Exception) { throw DebateRequestException(DebateFailure.UNAVAILABLE) }
      finally { bytes.fill(0) }
      call<AttachmentUploadStatus>(token, HttpMethod.Post, "$path/complete").also {
        it.validate(requestId, draft)
        check(it.status in setOf("ready", "consumed"))
      }
    }
  } catch (error: CancellationException) { throw error }
  catch (error: DebateRequestException) { throw error }
  catch (error: RecordImageException) { throw DebateRequestException(DebateFailure.STORAGE) }
  catch (_: Exception) { throw DebateRequestException(DebateFailure.INVALID_RESPONSE) }

  private suspend inline fun <reified T> call(token: String, method: HttpMethod, path: String,
    body: String? = null): T = try {
    check(mobileOpaqueValue.matches(token))
    client.prepareRequest("$RECORDS_ORIGIN/api/v1/debate-attachments/uploads$path") {
      this.method = method; bearerAuth(token); header(HttpHeaders.CacheControl, "no-store")
      if (body != null) { contentType(ContentType.Application.Json); setBody(body) }
    }.execute { response ->
      check(response.contentType()?.withoutParameters() == ContentType.Application.Json)
      val bytes = response.bodyAsChannel().readBuffer(65_537L).readByteArray()
      check(bytes.size <= 65_536)
      val text = bytes.decodeToString(throwOnInvalidSequence = true)
      if (response.status.value !in setOf(200, 201, 202)) {
        val code = json.decodeFromString<ErrorEnvelope>(text).error.code
        throw DebateRequestException(when {
          response.status.value == 401 || code == "GUILD_MEMBERSHIP_REQUIRED" -> DebateFailure.AUTH_REQUIRED
          code == "DEBATE_START_REAUTH_REQUIRED" -> DebateFailure.REAUTH_REQUIRED
          code == "ATTACHMENT_NOT_FOUND" -> DebateFailure.NOT_FOUND
          code == "ATTACHMENT_EXPIRED" -> DebateFailure.ATTACHMENT_EXPIRED
          code == "ATTACHMENT_NOT_READY" -> DebateFailure.ATTACHMENT_NOT_READY
          code == "ATTACHMENT_INVALID" -> DebateFailure.ATTACHMENT_INVALID
          response.status.value == 403 -> DebateFailure.FORBIDDEN
          response.status.value == 409 -> DebateFailure.CONFLICT
          else -> DebateFailure.UNAVAILABLE
        })
      }
      json.decodeFromString<T>(text)
    }
  } catch (error: CancellationException) { throw error }
  catch (error: DebateRequestException) { throw error }
  catch (_: Exception) { throw DebateRequestException(DebateFailure.INVALID_RESPONSE) }

  override fun close() { client.close(); engine.close() }
  @Serializable private class UploadRequest(val requestId: String, val attachmentId: String,
    val contentType: String, val sizeBytes: Int, val sha256: String)
  @Serializable private class ErrorEnvelope(val error: ErrorBody)
  @Serializable private class ErrorBody(val code: String)
}
