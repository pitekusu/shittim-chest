package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import androidx.room3.Room
import androidx.sqlite.driver.AndroidSQLiteDriver
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.File
import java.nio.file.Files
import java.util.UUID
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MomotalkRepositoryTest {
  private val owner = "u".repeat(43)
  private val token = "t".repeat(43)
  private val json = Json { encodeDefaults = true }
  private val headers = headersOf(HttpHeaders.ContentType, "application/json")
  private lateinit var app: Context
  private lateinit var context: Context
  private lateinit var directory: File
  private lateinit var databaseName: String
  private lateinit var database: EncryptedRecordsDatabase
  private lateinit var keys: KeystorePrivateKeyStore
  private lateinit var store: EncryptedRecordStore
  private var active = true

  @Before fun setup() = runBlocking {
    app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "momotalk-cache-test-").toFile()
    val testPackage = "${app.packageName}.test.${UUID.randomUUID()}"
    context = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir() = directory
      override fun getPackageName() = testPackage
    }
    keys = KeystorePrivateKeyStore(context)
    databaseName = "momotalk-cache-test-${UUID.randomUUID()}.db"
    openDatabase()
    RecordCacheAccount.lock.withLock { RecordCacheAccount(context, keys, database).activate(owner) }
    active = true
  }
  @After fun teardown() {
    database.close(); keys.clear(); app.deleteDatabase(databaseName); directory.deleteRecursively()
  }

  @Test fun weekAndRoomPaginationSurviveOfflineReopenAndFailedRefreshKeepsSnapshot() = runBlocking {
    var fail = false
    repo(MockEngine { request ->
      if (fail) respond("", HttpStatusCode.ServiceUnavailable)
      else respond(when {
        request.url.encodedPath.endsWith("/weeks") -> json.encodeToString(if (request.url.parameters["cursor"] == null)
          MomotalkWeeksPage(weeks = listOf(momotalkDataWeek()), nextCursor = "2026-10-18") else
          MomotalkWeeksPage(weeks = listOf(momotalkDataWeek("2026-10-11"))))
        else -> json.encodeToString(MomotalkRoomsPage(week = momotalkDataWeek(), rooms = listOf(momotalkDataRoom())))
      }, headers = headers)
    }).use { repository ->
      assertEquals(1, repository.refreshWeeks(token, owner).size)
      assertEquals(2, repository.appendWeeks(token, owner, "2026-10-18").size)
      repository.refreshRooms(token, owner, "2026-10-18")
      fail = true
      assertMomotalkFailure(MomotalkFailure.UNAVAILABLE) { repository.refreshWeeks(token, owner) }
      assertEquals(2, repository.cachedWeeks(owner).size)
    }
    openDatabase()
    repo(MockEngine { error("Offline snapshots must not access HTTP") }).use { repository ->
      assertEquals(listOf("2026-10-18", "2026-10-11"), repository.cachedWeeks(owner).flatMap { it.value.weeks }.map { it.weekId })
      assertEquals(MOMOTALK_DATA_ROOM, repository.cachedRooms(owner, "2026-10-18").single().value.rooms.single().roomId)
    }
  }

  @Test fun conversationSavesBeforeOptionalPhotosAndOnlyBytesPersistWithoutSignedUrls() = runBlocking {
    var imageCalls = 0
    val imageBytes = momotalkDataWebp()
    repo(MockEngine { request ->
      if (request.url.host.endsWith("amazonaws.com")) {
        imageCalls++
        assertTrue(request.url.encodedPath.endsWith("selfie-thumb.webp"))
        assertFalse(request.headers.contains(HttpHeaders.Authorization)); assertFalse(request.headers.contains(HttpHeaders.Cookie))
        respond(imageBytes, headers = headersOf(HttpHeaders.ContentType, "image/webp"))
      } else respond(json.encodeToString(momotalkDataDetail()), headers = headers)
    }).use { repository ->
      val initial = repository.refreshRoom(token, owner, "2026-10-18", MOMOTALK_DATA_ROOM).value
      assertEquals(1, initial.messages.size); assertNull(initial.images.single().bytes); assertNull(initial.images.single().url)
      assertEquals(0, imageCalls)
      val id = store.recordIds(owner, CachedRecordPart.MOMOTALK_DETAIL).single()
      val plain = store.load(owner, id, CachedRecordPart.MOMOTALK_DETAIL)!!
      try { assertFalse(plain.decodeToString().contains("signature")); assertFalse(plain.decodeToString().contains("amazonaws.com")) }
      finally { plain.fill(0) }
      repository.warmRoomImages(owner, "2026-10-18", MOMOTALK_DATA_ROOM)
      assertTrue(repository.cachedRoom(owner, "2026-10-18", MOMOTALK_DATA_ROOM)!!.value.images.single().bytes!!.contentEquals(imageBytes))
      assertEquals(1, imageCalls)
      repository.warmRoomImages(owner, "2026-10-18", MOMOTALK_DATA_ROOM)
      assertEquals(1, imageCalls)
    }
    openDatabase()
    repo(MockEngine { error("Media restored locally") }).use { repository ->
      val detail = repository.cachedRoom(owner, "2026-10-18", MOMOTALK_DATA_ROOM)!!.value
      assertNotNull(detail.images.single().bytes); assertNull(detail.images.single().thumbnailUrl)
    }
  }

  @Test fun optionalImageFailureKeepsConversationAndLateLogoutCannotCommit() = runBlocking {
    var logout = false
    repo(MockEngine { request ->
      if (request.url.host.endsWith("amazonaws.com")) respond("", HttpStatusCode.Forbidden)
      else {
        if (logout) active = false
        respond(json.encodeToString(momotalkDataDetail()), headers = headers)
      }
    }).use { repository ->
      repository.refreshRoom(token, owner, "2026-10-18", MOMOTALK_DATA_ROOM)
      repository.warmRoomImages(owner, "2026-10-18", MOMOTALK_DATA_ROOM)
      assertEquals(1, repository.cachedRoom(owner, "2026-10-18", MOMOTALK_DATA_ROOM)!!.value.messages.size)
      assertTrue(store.recordIds(owner, CachedRecordPart.MOMOTALK_MEDIA).isEmpty())
      logout = true
      assertMomotalkFailure(MomotalkFailure.AUTH_REQUIRED) { repository.refreshRoom(token, owner, "2026-10-18", MOMOTALK_DATA_ROOM) }
      assertMomotalkFailure(MomotalkFailure.AUTH_REQUIRED) { repository.cachedRoom(owner, "2026-10-18", MOMOTALK_DATA_ROOM) }
    }
  }

  @Test fun differentAccountCannotReadPriorSnapshotOrMedia() = runBlocking {
    repo(MockEngine { respond(json.encodeToString(momotalkDataDetail()), headers = headers) }).use { repository ->
      repository.refreshRoom(token, owner, "2026-10-18", MOMOTALK_DATA_ROOM)
      RecordCacheAccount.lock.withLock { RecordCacheAccount(context, keys, database).activate("v".repeat(43)) }
      assertMomotalkFailure(MomotalkFailure.STORAGE_UNAVAILABLE) { repository.cachedRoom(owner, "2026-10-18", MOMOTALK_DATA_ROOM) }
    }
  }
  private fun openDatabase() {
    database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName).setDriver(AndroidSQLiteDriver()).build()
    store = EncryptedRecordStore(database, RecordDataKeyProtector(keys))
  }
  private fun repo(engine: MockEngine) = MomotalkRepository(RecordsReadClient(engine), store, database,
    RecordCacheAccount(context, keys, database), { active && it == owner })
}
