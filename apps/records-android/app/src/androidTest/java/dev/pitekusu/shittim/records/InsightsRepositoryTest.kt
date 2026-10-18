package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import android.graphics.Bitmap
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
import java.io.ByteArrayOutputStream
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
class InsightsRepositoryTest {
  private val accountId = "u".repeat(43)
  private val token = "t".repeat(43)
  private val json = Json { encodeDefaults = true }
  private val header = headersOf(HttpHeaders.ContentType, "application/json")
  private lateinit var context: Context
  private lateinit var app: Context
  private lateinit var directory: File
  private lateinit var databaseName: String
  private lateinit var database: EncryptedRecordsDatabase
  private lateinit var keys: KeystorePrivateKeyStore
  private lateinit var store: EncryptedRecordStore
  private var active = true

  @Before fun setUp() = runBlocking {
    app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "insights-cache-test-").toFile()
    context = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = "${app.packageName}.test.${UUID.randomUUID()}"
    }
    keys = KeystorePrivateKeyStore(context)
    databaseName = "insights-cache-test-${UUID.randomUUID()}.db"
    openDatabase()
    RecordCacheAccount.lock.withLock { RecordCacheAccount(context, keys, database).activate(accountId) }
    active = true
  }
  @After fun tearDown() {
    database.close(); keys.clear(); app.deleteDatabase(databaseName); directory.deleteRecursively()
  }

  @Test fun keepsPagesAcrossOfflineReopenAndRefreshCommitsOneNewGeneration() = runBlocking {
    var calls = 0
    repository(MockEngine { request ->
      calls++
      respond(json.encodeToString(if (request.url.parameters["cursor"] == null)
        insightTestParticipants(nextCursor = "next") else insightTestParticipants(rank = 2, score = 400)), headers = header)
    }).use { repo ->
      assertEquals(1, repo.refreshParticipants(token, accountId).size)
      val appended = repo.appendParticipants(token, accountId, "next")
      assertEquals(listOf(500, 400), appended.flatMap { it.value.rankings.first().entries }.map { it.score })
      assertEquals(3, store.recordIds(accountId, CachedRecordPart.INSIGHTS_AFFECTION).size)
    }
    assertEquals(2, calls)
    openDatabase()
    repository(MockEngine { error("Offline read must not access HTTP") }).use { repo ->
      val pages = repo.cachedParticipants(accountId)
      assertEquals(2, pages.size)
      assertTrue(pages.all { it.value.rankings.first().entries.first().avatar.url == null })
    }
    openDatabase()
    repository(MockEngine { respond(json.encodeToString(insightTestParticipants("2026-10-18T01:00:00Z")), headers = header) }).use { repo ->
      assertEquals(1, repo.refreshParticipants(token, accountId).size)
      assertEquals("2026-10-18T01:00:00Z", repo.cachedParticipants(accountId).single().value.generatedAt)
      assertEquals(2, store.recordIds(accountId, CachedRecordPart.INSIGHTS_AFFECTION).size)
    }
  }

  @Test fun failedFirstPageSaveAndExpiredAppendKeepLastCommittedSnapshot() = runBlocking {
    var mode = 0
    repository(MockEngine {
      when (mode) {
        1 -> respond(json.encodeToString(insightTestParticipants("2026-10-18T01:00:00Z", name = "名".repeat(400_000))), headers = header)
        2 -> respond("""{"error":{"code":"CURSOR_INVALID"}}""", HttpStatusCode.BadRequest, header)
        else -> respond(json.encodeToString(insightTestParticipants(nextCursor = "next")), headers = header)
      }
    }).use { repo ->
      repo.refreshParticipants(token, accountId)
      mode = 1
      assertInsightsFailure(InsightsFailure.STORAGE_UNAVAILABLE) { repo.refreshParticipants(token, accountId) }
      assertEquals("2026-10-18T00:00:00Z", repo.cachedParticipants(accountId).single().value.generatedAt)
      mode = 2
      assertInsightsFailure(InsightsFailure.CURSOR_INVALID) { repo.appendParticipants(token, accountId, "next") }
      assertEquals(1, repo.cachedParticipants(accountId).size)
    }
  }

  @Test fun rejectsMixedGenerationAndOutOfOrderGlobalRankBeforeSavingAppend() = runBlocking {
    var mode = 0
    repository(MockEngine { request ->
      val page = when {
        request.url.parameters["cursor"] == null -> insightTestParticipants(nextCursor = "next")
        mode == 1 -> insightTestParticipants("2026-10-18T01:00:00Z", rank = 2, score = 400)
        else -> insightTestParticipants(rank = 3, score = 400)
      }
      respond(json.encodeToString(page), headers = header)
    }).use { repo ->
      repo.refreshParticipants(token, accountId)
      mode = 1
      assertInsightsFailure(InsightsFailure.INVALID_RESPONSE) { repo.appendParticipants(token, accountId, "next") }
      mode = 2
      assertInsightsFailure(InsightsFailure.INVALID_RESPONSE) { repo.appendParticipants(token, accountId, "next") }
      assertEquals(1, repo.cachedParticipants(accountId).size)
      assertEquals(2, store.recordIds(accountId, CachedRecordPart.INSIGHTS_AFFECTION).size)
    }
  }

  @Test fun isolatesCostPeriodsSameNameRequestersAndRecordSyncParts() = runBlocking {
    repository(MockEngine { request ->
      val payload = if (request.url.encodedPath.endsWith("/costs")) {
        json.encodeToString(insightTestCosts(InsightsPeriod.entries.first { it.wireValue == request.url.parameters["period"] }))
      } else json.encodeToString(insightTestRequesters())
      respond(payload, headers = header)
    }).use { repo ->
      InsightsPeriod.entries.forEach { repo.refreshCosts(token, accountId, it) }
      InsightsPeriod.entries.forEach { assertEquals(it, repo.cachedCosts(accountId, it)?.value?.period) }
      assertEquals(4, store.recordIds(accountId, CachedRecordPart.INSIGHTS_COSTS).size)
      assertEquals(2, repo.refreshRequesters(token, accountId).single().value.requesters.size)
      // Part-limited reclamation cannot remove another consumer even with the same opaque row ID.
      val pageId = store.recordIds(accountId, CachedRecordPart.INSIGHTS_REQUESTERS).first { !it.contains("manifest") }
      store.save(accountId, pageId, CachedRecordPart.LIST, byteArrayOf(1))
      repo.refreshRequesters(token, accountId)
      assertNotNull(store.load(accountId, pageId, CachedRecordPart.LIST))
    }
  }

  @Test fun logoutDuringRemoteReadDiscardsLateResponseAndCacheReadsRequireActiveOwner() = runBlocking {
    repository(MockEngine {
      active = false
      respond(json.encodeToString(insightTestRankings()), headers = header)
    }).use { repo ->
      assertInsightsFailure(InsightsFailure.AUTH_REQUIRED) { repo.refreshRankings(token, accountId) }
      assertTrue(store.recordIds(accountId, CachedRecordPart.INSIGHTS_RANKINGS).isEmpty())
      assertInsightsFailure(InsightsFailure.AUTH_REQUIRED) { repo.cachedRankings(accountId) }
      active = true
      RecordCacheAccount.lock.withLock { RecordCacheAccount(context, keys, database).activate("v".repeat(43)) }
      assertInsightsFailure(InsightsFailure.STORAGE_UNAVAILABLE) { repo.cachedRankings(accountId) }
    }
  }

  @Test fun cachesParticipantAndRequesterAvatarsSeparatelyAndRestoresBytesWithoutNetwork() = runBlocking {
    val bitmap = Bitmap.createBitmap(12, 12, Bitmap.Config.ARGB_8888)
    val png = try { ByteArrayOutputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it); it.toByteArray() } }
    finally { bitmap.recycle() }
    val url = "https://fixture.s3.ap-northeast-1.amazonaws.com/participants/arona/avatar.png?signature=synthetic"
    var imageCalls = 0
    repository(MockEngine { request ->
      if (request.url.host.endsWith("amazonaws.com")) {
        imageCalls++
        assertFalse(request.headers.contains(HttpHeaders.Authorization)); assertFalse(request.headers.contains(HttpHeaders.Cookie))
        respond(png, headers = headersOf(HttpHeaders.ContentType, "image/png"))
      } else respond(json.encodeToString(insightTestRankings(RecordAvatar(url, "cyan"))), headers = header)
    }).use { repo ->
      val initial = repo.refreshRankings(token, accountId).value.wins.first().avatar
      assertNull(initial.bytes); assertNull(initial.url)
      assertEquals(0, imageCalls)
      repo.warmRankingsAvatars(accountId)
      val avatar = repo.cachedRankings(accountId)!!.value.wins.first().avatar
      assertNotNull(avatar.bytes); assertNull(avatar.url)
      assertEquals(1, imageCalls)
      assertEquals(1, store.recordIds(accountId, CachedRecordPart.INSIGHTS_AVATAR).size)
      assertTrue(store.recordIds(accountId, CachedRecordPart.AVATAR).isEmpty())
    }
    openDatabase()
    repository(MockEngine { error("No HTTP after reopening encrypted snapshots") }).use { repo ->
      assertNotNull(repo.cachedRankings(accountId)?.value?.wins?.first()?.avatar?.bytes)
    }
  }

  private fun openDatabase() {
    database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName).setDriver(AndroidSQLiteDriver()).build()
    store = EncryptedRecordStore(database, RecordDataKeyProtector(keys))
  }
  private fun repository(engine: MockEngine) = InsightsRepository(RecordsReadClient(engine), store, database,
    RecordCacheAccount(context, keys, database), { active && it == accountId })
}
