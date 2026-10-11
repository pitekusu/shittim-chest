package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import java.io.ByteArrayOutputStream
import java.time.Instant
import java.util.Base64
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DebateAttachmentsClientTest {
  private val request = "11111111-2222-4333-8444-abcdefabcdef"
  private val imageId = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
  private val token = "t".repeat(43)
  private val headers = headersOf(HttpHeaders.ContentType, "application/json")
  private fun source(): ByteArray {
    val bitmap = Bitmap.createBitmap(8, 4, Bitmap.Config.ARGB_8888)
    return try { ByteArrayOutputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it); it.toByteArray() } }
    finally { bitmap.recycle() }
  }
  private fun image(bytes: ByteArray) = DraftAttachment(imageId, "image/png", bytes.size, attachmentDigest(bytes), 8, 4)
  private fun status(draft: DraftAttachment, state: String = "ready") = """{
    "schemaVersion":1,"attachmentId":"$imageId","requestId":"$request","status":"$state",
    "image":{"attachmentId":"$imageId","contentType":"image/png","sizeBytes":${draft.sizeBytes},
      "sha256":"${draft.sha256}","width":8,"height":4}}
    """.trimIndent()

  @Test fun readyOriginalIsNeverUploadedAgain() = runBlocking {
    val draft = image(source())
    var calls = 0
    DebateAttachmentsClient(MockEngine { network ->
      calls++
      assertEquals("GET", network.method.value)
      assertTrue(network.url.encodedPath.endsWith(imageId))
      assertEquals("Bearer $token", network.headers[HttpHeaders.Authorization])
      respond(status(draft), HttpStatusCode.OK, headers)
    }).use { it.prepare(token, request, listOf(draft)) { error("ready image must not read draft bytes") } }
    assertEquals(1, calls)
  }

  @Test fun ambiguousS3UploadIsCompletedBeforeAnotherUploadAttempt() = runBlocking {
    val draft = image(source())
    val methods = mutableListOf<String>()
    DebateAttachmentsClient(MockEngine { network ->
      methods += network.method.value
      if (network.method.value == "GET") respond("""{"schemaVersion":1,"attachmentId":"$imageId",
        "requestId":"$request","status":"reserved","image":null}""", HttpStatusCode.OK, headers)
      else {
        assertTrue(network.url.encodedPath.endsWith("/$imageId/complete"))
        respond(status(draft), HttpStatusCode.OK, headers)
      }
    }).use { it.prepare(token, request, listOf(draft)) { error("completed prior upload must not be replayed") } }
    assertEquals(listOf("GET", "POST"), methods)
  }

  @Test fun originalPostUsesSignedFieldsAndS3GetsNoAppCredentials() = runBlocking {
    val bytes = source()
    val draft = image(bytes)
    val checksum = Base64.getEncoder().encodeToString(draft.sha256.chunked(2).map { it.toInt(16).toByte() }.toByteArray())
    var calls = 0
    DebateAttachmentsClient(MockEngine { network ->
      calls++
      when (calls) {
        1 -> respond("""{"error":{"code":"ATTACHMENT_NOT_FOUND"}}""", HttpStatusCode.NotFound, headers)
        2 -> respond("""{"schemaVersion":1,"attachmentId":"$imageId","method":"POST",
          "uploadUrl":"https://shittim-chest-production-records-memorial-upload-000000000000.s3.ap-northeast-1.amazonaws.com/",
          "expiresAt":"${Instant.now().plusSeconds(600)}","fields":{"key":"debate-uploads/synthetic.bin",
          "Content-Type":"image/png","x-amz-checksum-sha256":"$checksum","x-amz-algorithm":"AWS4-HMAC-SHA256",
          "x-amz-credential":"synthetic","x-amz-date":"synthetic","policy":"synthetic","x-amz-signature":"synthetic"}}""",
          HttpStatusCode.OK, headers)
        3 -> {
          assertTrue(network.url.host.contains("records-memorial-upload"))
          assertNull(network.headers[HttpHeaders.Authorization])
          assertNull(network.headers[HttpHeaders.Cookie])
          assertEquals("POST", network.method.value)
          respond("", HttpStatusCode.NoContent)
        }
        4 -> respond(status(draft), HttpStatusCode.OK, headers)
        else -> error("unexpected automatic replay")
      }
    }).use { it.prepare(token, request, listOf(draft)) { bytes.copyOf() } }
    assertEquals(4, calls)
  }

  @Test fun expiredReservationDoesNotReadOrSendTheImage() = runBlocking {
    val draft = image(source())
    DebateAttachmentsClient(MockEngine { respond("""{"schemaVersion":1,"attachmentId":"$imageId",
      "requestId":"$request","status":"expired","image":null}""", HttpStatusCode.OK, headers) }).use {
      try { it.prepare(token, request, listOf(draft)) { error("expired image must not be sent") }; fail("expired reservation") }
      catch (error: DebateRequestException) { assertEquals(DebateFailure.ATTACHMENT_EXPIRED, error.failure) }
    }
  }
}
