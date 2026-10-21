package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.ByteArrayOutputStream
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MomotalkReadClientTest {
  private val token = "t".repeat(43)
  private val json = Json { encodeDefaults = true }
  private val header = headersOf(HttpHeaders.ContentType, "application/json")

  @Test fun readsUseBearerNoCookiesAndNegotiateAllSelfieSubjects() = runBlocking {
    val paths = mutableListOf<String>()
    RecordsReadClient(MockEngine { request ->
      assertEquals("Bearer $token", request.headers[HttpHeaders.Authorization])
      assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
      assertFalse(request.headers.contains(HttpHeaders.Cookie))
      paths += request.url.encodedPath + "?" + request.url.encodedQuery
      respond(when {
        request.url.encodedPath.endsWith("/weeks") -> json.encodeToString(MomotalkWeeksPage(weeks = listOf(momotalkDataWeek())))
        request.url.encodedPath.endsWith("/rooms") -> json.encodeToString(MomotalkRoomsPage(week = momotalkDataWeek(), rooms = listOf(momotalkDataRoom())))
        else -> json.encodeToString(momotalkDataDetail())
      }, headers = header)
    }).use { client ->
      assertEquals("2026-10-18", client.momotalkWeeks(token).weeks.single().weekId)
      assertEquals(1, client.momotalkRooms(token, "2026-10-18").rooms.size)
      assertEquals(3, client.momotalkRoom(token, "2026-10-18", MOMOTALK_DATA_ROOM).images.single().subjects.size)
    }
    assertEquals(listOf("/api/v1/momotalk/weeks?limit=20",
      "/api/v1/momotalk/weeks/2026-10-18/rooms?limit=20",
      "/api/v1/momotalk/weeks/2026-10-18/rooms/$MOMOTALK_DATA_ROOM?contract=momotalk-selfie-v2"), paths)
  }

  @Test fun rejectsWrongIdentityUntrustedMediaDuplicateSpeakersAndUnnegotiatedSubjects() = runBlocking {
    val detail = momotalkDataDetail()
    val payload = json.encodeToString(detail)
    listOf(
      payload.replace(MOMOTALK_DATA_ROOM, "b".repeat(43)),
      payload.replace("participant-c", "participant-b"),
      payload.replace(MOMOTALK_DATA_HOST, "example.invalid"),
      payload.replace("\"id\":1", "\"id\":2"),
      payload.replace("\"schemaVersion\":1,", ""),
      json.encodeToString(detail.copy(images = listOf(detail.images.single().copy(subjects = emptyList())))),
    ).forEach { candidate ->
      RecordsReadClient(MockEngine { respond(candidate, headers = header) }).use { client ->
        assertMomotalkFailure(MomotalkFailure.INVALID_RESPONSE) { client.momotalkRoom(token, "2026-10-18", MOMOTALK_DATA_ROOM) }
      }
    }
  }

  @Test fun rejectsLoopingPagesAndUnsafeIdentifiersBeforeHttp() = runBlocking {
    var calls = 0
    RecordsReadClient(MockEngine {
      calls++
      respond(json.encodeToString(MomotalkWeeksPage(weeks = emptyList(), nextCursor = "2026-10-18")), headers = header)
    }).use { client ->
      assertMomotalkFailure(MomotalkFailure.INVALID_RESPONSE) { client.momotalkWeeks(token, "2026-10-18") }
      assertMomotalkFailure(MomotalkFailure.INVALID_RESPONSE) { client.momotalkRoom(token, "../", MOMOTALK_DATA_ROOM) }
      assertMomotalkFailure(MomotalkFailure.AUTH_REQUIRED) { client.momotalkWeeks("invalid") }
    }
    assertEquals(1, calls)
  }

  @Test fun separatesAuthenticationMissingConversationCursorAndUnavailableResponses() = runBlocking {
    listOf(
      Triple(HttpStatusCode.Unauthorized, "AUTHENTICATION_REQUIRED", MomotalkFailure.AUTH_REQUIRED),
      Triple(HttpStatusCode.NotFound, "MOMOTALK_NOT_FOUND", MomotalkFailure.NOT_FOUND),
      Triple(HttpStatusCode.BadRequest, "CURSOR_INVALID", MomotalkFailure.CURSOR_INVALID),
      Triple(HttpStatusCode.ServiceUnavailable, "MOMOTALK_UNAVAILABLE", MomotalkFailure.UNAVAILABLE),
    ).forEach { (status, code, expected) ->
      RecordsReadClient(MockEngine { respond("""{"error":{"code":"$code"}}""", status, header) }).use { client ->
        assertMomotalkFailure(expected) { client.momotalkWeeks(token) }
      }
    }
  }

  @Test fun s3PicturesNeverReceiveSessionAndRejectRedirectOrWrongRoom() = runBlocking {
    val bytes = momotalkDataWebp()
    RecordsReadClient(MockEngine { request ->
      assertFalse(request.headers.contains(HttpHeaders.Authorization)); assertFalse(request.headers.contains(HttpHeaders.Cookie))
      respond(bytes, headers = headersOf(HttpHeaders.ContentType, "image/webp"))
    }).use { client ->
      assertTrue(client.momotalkImage(MOMOTALK_DATA_IMAGE, "2026-10-18", MOMOTALK_DATA_ROOM).contentEquals(bytes))
      assertMomotalkFailure(MomotalkFailure.INVALID_RESPONSE) { client.momotalkImage(MOMOTALK_DATA_IMAGE, "2026-10-18", "b".repeat(43)) }
      assertMomotalkFailure(MomotalkFailure.INVALID_RESPONSE) { client.momotalkImage("https://example.invalid/picture.webp", "2026-10-18", MOMOTALK_DATA_ROOM) }
    }
    RecordsReadClient(MockEngine { respond("", HttpStatusCode.Found, headersOf(HttpHeaders.Location, "https://example.invalid")) }).use { client ->
      assertMomotalkFailure(MomotalkFailure.UNAVAILABLE) { client.momotalkImage(MOMOTALK_DATA_IMAGE, "2026-10-18", MOMOTALK_DATA_ROOM) }
    }
  }
}

internal val MOMOTALK_DATA_ROOM = "a".repeat(43)
internal val MOMOTALK_DATA_HOST = "shittim-chest-production-records-media-${"123456" + "789012"}.s3.ap-northeast-1.amazonaws.com"
internal val MOMOTALK_DATA_IMAGE = "https://$MOMOTALK_DATA_HOST/momotalk/images/2026-10-18/$MOMOTALK_DATA_ROOM/selfie.webp?signature=synthetic"
internal fun momotalkDataWeek(id: String = "2026-10-18") = MomotalkWeek(id,
  "2026-10-11T09:00:00Z", "2026-10-18T09:00:00Z", "2026-10-18T11:00:00Z")
internal fun momotalkDataRoom(id: String = MOMOTALK_DATA_ROOM) = MomotalkRoomSummary(id,
  MomotalkRequester("架空の依頼者", RecordAvatar(null, "cyan")), 3, "ready")
internal fun momotalkDataDetail() = MomotalkRoomDetail(week = momotalkDataWeek(), room = momotalkDataRoom(),
  participants = MOMOTALK_PARTICIPANTS.map { MomotalkParticipant(it, it, RecordAvatar(null, "cyan")) },
  messages = listOf(MomotalkMessage(1, "participant-a", "架空の会話です。")),
  images = listOf(MomotalkImage("participant-a", "happy", MOMOTALK_PARTICIPANTS.map { MomotalkImageSubject(it, "happy") },
    "ready", MOMOTALK_DATA_IMAGE, MOMOTALK_DATA_IMAGE.replace("selfie.webp", "selfie-thumb.webp"), MOMOTALK_DATA_IMAGE)))
internal fun momotalkDataWebp(): ByteArray {
  val bitmap = Bitmap.createBitmap(12, 18, Bitmap.Config.ARGB_8888)
  return try { ByteArrayOutputStream().use { bitmap.compress(Bitmap.CompressFormat.WEBP_LOSSLESS, 100, it); it.toByteArray() } }
  finally { bitmap.recycle() }
}
internal suspend fun assertMomotalkFailure(expected: MomotalkFailure, block: suspend () -> Unit) {
  try { block(); fail("Expected $expected") }
  catch (error: MomotalkException) { assertEquals(expected, error.failure) }
}
