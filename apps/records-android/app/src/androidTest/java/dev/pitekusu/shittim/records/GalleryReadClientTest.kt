package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.ByteArrayOutputStream
import java.util.Base64
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class GalleryReadClientTest {
  private val token = "t".repeat(43)
  private val acquired = "a".repeat(43)
  private val locked = "b".repeat(43)
  private val retired = "c".repeat(43)
  private val stamp = "2026-10-18T09:00:00.000001Z"
  private val latest = cardId('c')
  private val legacy = cardId('b')
  private val oldest = cardId('a')
  private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json")
  private val nextCursor = "cursor.signature"
  private val mediaHost = "shittim-chest-production-records-media-${"123456" + "789012"}.s3.ap-northeast-1.amazonaws.com"

  @Test fun combinesPagesInStorageOrderAndKeepsLockedAndRetiredStudentsAccessible() = runBlocking {
    RecordsReadClient(MockEngine { request ->
      assertEquals("Bearer $token", request.headers[HttpHeaders.Authorization])
      assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
      assertFalse(request.headers.contains(HttpHeaders.Cookie))
      when (request.url.encodedPath) {
        "/api/v1/gallery/roster" -> respond(roster(), headers = jsonHeaders)
        "/api/v1/gallery" -> {
          assertEquals("gallery-collection-v1", request.url.parameters["contract"])
          when (request.url.parameters["cursor"]) {
            null -> respond(page(card(latest, acquired), card(legacy, retired, rarity = "null"),
              cursor = nextCursor), headers = jsonHeaders)
            nextCursor -> respond(page(card(oldest, acquired, thumbnailReady = false)), headers = jsonHeaders)
            else -> error("Unexpected page request")
          }
        }
        else -> error("Unexpected request")
      }
    }).use { client ->
      val collection = client.galleryCollection(token)
      assertEquals(listOf(latest, legacy, oldest), collection.cards.map { it.cardId })
      val revealed = collection.entries.first()
      assertEquals("架空の生徒", revealed.studentName)
      assertEquals("架空の学園", revealed.schoolName)
      assertEquals(listOf(oldest, latest), revealed.cards.map { it.cardId })
      assertFalse(revealed.cards.first().thumbnailReady)
      assertEquals(0, revealed.firstAcquisitionOrder)
      val hidden = collection.entries.last()
      assertNull(hidden.studentName)
      assertNull(hidden.schoolName)
      assertTrue(hidden.cards.isEmpty())
      assertNull(collection.cards[1].rarity)
    }
  }

  @Test fun rejectsLoopingCursorsAndRosterRevealingAnUnacquiredSecret() = runBlocking {
    RecordsReadClient(MockEngine { request ->
      respond(if (request.url.encodedPath.endsWith("/roster")) roster() else page(cursor = nextCursor),
        headers = jsonHeaders)
    }).use { client ->
      assertEquals(GalleryReadFailure.INVALID_RESPONSE, failure { client.galleryCollection(token) }.failure)
    }
    RecordsReadClient(MockEngine { request ->
      val payload = if (request.url.encodedPath.endsWith("/roster")) roster().replace(
        """"schoolName":null,"rarity":0""", """"schoolName":"架空の学園","rarity":0""") else page()
      respond(payload, headers = jsonHeaders)
    }).use { client ->
      assertEquals(GalleryReadFailure.INVALID_RESPONSE, failure { client.galleryCollection(token) }.failure)
    }
  }

  @Test fun distinguishesConfirmed401FromInvalidLocalCredential() = runBlocking {
    RecordsReadClient(MockEngine { respond("", status = HttpStatusCode.Unauthorized) }).use { client ->
      val server = failure { client.galleryOriginal(token, oldest) }
      assertEquals(GalleryReadFailure.AUTH_REQUIRED, server.failure)
      assertTrue(server.serverRejectedSession)
      val local = failure { client.galleryOriginal("invalid", oldest) }
      assertEquals(GalleryReadFailure.AUTH_REQUIRED, local.failure)
      assertFalse(local.serverRejectedSession)
    }
  }

  @Test fun thumbnailsRequireRequestedIdsAndOriginalImagesReceiveNoToken() = runBlocking {
    val url = "https://$mediaHost/student-gacha/results/${"a".repeat(32)}.png?signature=synthetic"
    val expiry = "2099-10-18T09:00:00Z"
    val png = Bitmap.createBitmap(1792, 828, Bitmap.Config.ARGB_8888).let { bitmap ->
      try {
        ByteArrayOutputStream().use { output ->
          check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output))
          output.toByteArray()
        }
      } finally {
        bitmap.recycle()
      }
    }
    RecordsReadClient(MockEngine { request ->
      if (request.url.encodedPath.startsWith("/api/")) {
        assertEquals("Bearer $token", request.headers[HttpHeaders.Authorization])
        assertEquals("$oldest,$latest", request.url.parameters["ids"])
        respond("""{"schemaVersion":1,"items":[{"cardId":"$legacy","url":"$url","expiresAt":"$expiry"}]}""",
          headers = jsonHeaders)
      } else {
        assertFalse(request.headers.contains(HttpHeaders.Authorization))
        assertFalse(request.headers.contains(HttpHeaders.Cookie))
        assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
        respond(png, headers = headersOf(HttpHeaders.ContentType, "image/png"))
      }
    }).use { client ->
      assertEquals(GalleryReadFailure.INVALID_RESPONSE,
        failure { client.galleryThumbnails(token, listOf(oldest, latest)) }.failure)
      assertArrayEquals(png, client.galleryImage(url, expiry, oldest, thumbnail = false))
      assertEquals(GalleryReadFailure.INVALID_RESPONSE,
        failure { client.galleryImage(url, expiry, latest, thumbnail = false) }.failure)
      assertEquals(GalleryReadFailure.INVALID_RESPONSE,
        failure { client.galleryImage(url.replace(mediaHost, "example.invalid"), expiry, oldest, thumbnail = false) }.failure)
      assertEquals(GalleryReadFailure.UNAVAILABLE,
        failure { client.galleryImage(url, "2000-01-01T00:00:00Z", oldest, thumbnail = false) }.failure)
    }
  }

  private fun cardId(draw: Char): String = Base64.getUrlEncoder().withoutPadding()
    .encodeToString("$stamp#${draw.toString().repeat(32)}".toByteArray(Charsets.US_ASCII))

  private fun roster() = """{"schemaVersion":1,"items":[
    {"studentId":"$acquired","studentName":null,"schoolName":null,"rarity":4},
    {"studentId":"$locked","studentName":null,"schoolName":null,"rarity":0}
  ]}"""

  private fun card(cardId: String, studentId: String, rarity: String = "4", thumbnailReady: Boolean = true) =
    """{"cardId":"$cardId","studentId":"$studentId","studentName":"架空の生徒",
      "schoolName":"架空の学園","rarity":$rarity,"postedAt":"$stamp","performer":"participant-a",
      "collector":null,"isMine":false,"thumbnailReady":$thumbnailReady}"""

  private fun page(vararg cards: String, cursor: String? = null) =
    """{"schemaVersion":1,"contract":"gallery-collection-v1","items":[${cards.joinToString(",")}],
      "nextCursor":${cursor?.let { "\"$it\"" } ?: "null"}}"""

  private suspend fun failure(action: suspend () -> Unit): GalleryReadException =
    try {
      action()
      error("Expected gallery failure")
    } catch (error: GalleryReadException) {
      error
    }
}
