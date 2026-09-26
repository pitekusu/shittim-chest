package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import androidx.room3.Room
import androidx.sqlite.driver.AndroidSQLiteDriver
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import dev.pitekusu.shittim.records.storage.RecordCacheException
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.withLock
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordsRepositoryTest {
  private val recordId = "r".repeat(43)
  private val accountId = "u".repeat(43)
  private val token = "t".repeat(43)
  private lateinit var app: Context
  private lateinit var privateDirectory: File
  private lateinit var privateContext: Context
  private lateinit var privateKeys: KeystorePrivateKeyStore
  private lateinit var databaseName: String
  private var activeAccountId = accountId

  @Before fun setUp() {
    app = InstrumentationRegistry.getInstrumentation().targetContext
    privateDirectory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "repo-test-").toFile()
    privateContext = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir(): File = privateDirectory
      override fun getPackageName(): String = "${app.packageName}.test.${UUID.randomUUID()}"
    }
    privateKeys = KeystorePrivateKeyStore(privateContext)
    activeAccountId = accountId
    databaseName = "repo-test-${UUID.randomUUID()}.db"
  }

  @After fun tearDown() {
    privateKeys.clear()
    app.deleteDatabase(databaseName)
    privateDirectory.deleteRecursively()
  }

  @Test fun onlineListAndDetailSurviveRepositoryReopen() = runBlocking {
    repository(MockEngine { request ->
      val body = if (request.url.encodedPath.endsWith("/$recordId")) detail() else list()
      respond(body, headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { online ->
      assertEquals("架空の議題", online.recentRecords(token, accountId, null).items.single().questionPreview)
      assertEquals("架空の結論", (online.record(token, accountId, recordId) as RecordReadResult.Found)
        .preview.decision)
    }
    val marker = "架空の議題".toByteArray().toString(Charsets.ISO_8859_1)
    app.getDatabasePath(databaseName).parentFile!!.listFiles()!!
      .filter { it.name.startsWith(databaseName) && it.isFile }
      .forEach { file -> assertFalse(file.readBytes().toString(Charsets.ISO_8859_1).contains(marker)) }
    repository(MockEngine { error("unexpected_network_request") }).use { reopened ->
      assertEquals("架空の議題", reopened.cachedListEntry(accountId, recordId)?.questionPreview)
      assertEquals("アロナ", reopened.cachedRecord(accountId, recordId)?.winnerName)
      assertEquals("架空の結論", reopened.cachedRecord(accountId, recordId)?.decision)
    }
  }

  @Test fun anotherAccountReplacesTheOldKeyAndCiphertext() = runBlocking {
    val engine = { MockEngine { respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json")) } }
    repository(engine()).use { it.recentRecords(token, accountId, null) }
    val nextAccount = "v".repeat(43)
    activeAccountId = nextAccount
    repository(engine()).use { next ->
      assertEquals("架空の議題", next.recentRecords(token, nextAccount, null).items.single().questionPreview)
      assertEquals("架空の議題", next.cachedListEntry(nextAccount, recordId)?.questionPreview)
      try {
        next.cachedListEntry(accountId, recordId)
        fail("previous account must not regain access")
      } catch (error: RecordReadException) {
        assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure)
      }
    }
    val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    try {
      assertNull(EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
        .load(accountId, recordId, CachedRecordPart.LIST))
    } finally { database.close() }
  }

  @Test fun syncCheckpointSurvivesReopenAndRejectsInvalidOrUnauthorizedState() = runBlocking {
    val checkpoint = RecordSyncCheckpoint(cursor = "synthetic_cursor.signature",
      pendingIds = listOf(recordId), pageLoaded = true)
    repository(MockEngine { error("unexpected_network_request") }).use { records ->
      assertNull(records.syncCheckpoint(accountId))
      records.saveSyncCheckpoint(accountId, checkpoint)
      try {
        records.saveSyncCheckpoint(accountId, checkpoint.copy(pageLoaded = false))
        fail("invalid progress must not replace the saved checkpoint")
      } catch (error: RecordReadException) {
        assertEquals(RecordReadFailure.STORAGE_UNAVAILABLE, error.failure)
      }
    }
    val marker = checkpoint.cursor!!
    app.getDatabasePath(databaseName).parentFile!!.listFiles()!!
      .filter { it.name.startsWith(databaseName) && it.isFile }
      .forEach { assertFalse(it.readBytes().toString(Charsets.ISO_8859_1).contains(marker)) }
    repository(MockEngine { error("unexpected_network_request") }).use { records ->
      assertEquals(checkpoint, records.syncCheckpoint(accountId))
      activeAccountId = ""
      try {
        records.syncCheckpoint(accountId)
        fail("expired authorization must not read progress")
      } catch (error: RecordReadException) {
        assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure)
      }
    }
    activeAccountId = "v".repeat(43)
    repository(MockEngine { error("unexpected_network_request") }).use { records ->
      assertNull(records.syncCheckpoint(activeAccountId))
    }
  }

  @Test fun previousAccountsLateResponseCannotDeleteTheNewCache() = runBlocking {
    val started = CompletableDeferred<Unit>()
    val release = CompletableDeferred<Unit>()
    val delayed = repository(MockEngine {
      started.complete(Unit)
      release.await()
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    })
    try {
      val pending = async {
        try {
          delayed.recentRecords(token, accountId, null)
          fail("old session result must be rejected")
          null
        } catch (error: RecordReadException) { error.failure }
      }
      started.await()
      val nextAccount = "v".repeat(43)
      activeAccountId = nextAccount
      repository(MockEngine {
        respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
      }).use { next ->
        next.recentRecords(token, nextAccount, null)
        release.complete(Unit)
        assertEquals(RecordReadFailure.AUTH_REQUIRED, pending.await())
        assertEquals("架空の議題", next.cachedListEntry(nextAccount, recordId)?.questionPreview)
      }
    } finally {
      release.complete(Unit)
      delayed.close()
    }
  }

  @Test fun syncWalksEveryPageSeriallyAndFetchesOnlyMissingDetails() = runBlocking {
    val a = "a".repeat(43)
    val b = "b".repeat(43)
    val c = "c".repeat(43)
    val calls = mutableListOf<String>()
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      val body = if (id == "records") {
        val cursor = request.url.parameters["cursor"]
        calls.add("list:${cursor ?: "first"}")
        if (cursor == null) list(listOf(a, b), "next.signature") else list(listOf(b, c))
      } else { calls.add("detail:$id"); detail(id) }
      respond(body, headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      records.record(token, accountId, a)
      calls.clear()
      records.synchronize(token, accountId)
      assertEquals(listOf("list:first", "detail:$b", "list:next.signature", "detail:$c"), calls)
      assertEquals(true, records.syncCheckpoint(accountId)?.complete)
      for (id in listOf(a, b, c)) assertEquals("架空の結論", records.cachedRecord(accountId, id)?.decision)
      calls.clear()
      records.synchronize(token, accountId) // A completed pass checks new list pages, not old details.
      assertEquals(listOf("list:first", "list:next.signature"), calls)
    }
  }

  @Test fun failedOrCancelledSyncResumesThePendingDetailAfterReopen() = runBlocking {
    val nextId = "b".repeat(43)
    for (cancel in listOf(false, true)) {
      val entered = CompletableDeferred<Unit>()
      val release = CompletableDeferred<Unit>()
      val first = repository(MockEngine { request ->
        val id = request.url.encodedPath.substringAfterLast('/')
        if (id == nextId) {
          entered.complete(Unit)
          if (cancel) release.await() else throw IOException("synthetic_failure")
        }
        respond(if (id == "records") list(listOf(recordId, nextId)) else detail(id),
          headers = headersOf(HttpHeaders.ContentType, "application/json"))
      })
      try {
        if (cancel) {
          val job = launch { first.synchronize(token, accountId) }
          entered.await()
          job.cancelAndJoin()
        } else {
          try { first.synchronize(token, accountId); fail("network failure must stop sync") }
          catch (error: RecordReadException) { assertEquals(RecordReadFailure.UNAVAILABLE, error.failure) }
        }
        assertEquals(listOf(nextId), first.syncCheckpoint(accountId)?.pendingIds)
        assertEquals(false, first.syncCheckpoint(accountId)?.complete)
      } finally { first.close() }
      val resumed = mutableListOf<String>()
      repository(MockEngine { request ->
        val id = request.url.encodedPath.substringAfterLast('/')
        resumed.add(id)
        respond(detail(id), headers = headersOf(HttpHeaders.ContentType, "application/json"))
      }).use { records ->
        records.synchronize(token, accountId)
        assertEquals(listOf(nextId), resumed)
        assertEquals(true, records.syncCheckpoint(accountId)?.complete)
      }
      val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
        .setDriver(AndroidSQLiteDriver()).build()
      try { RecordCacheAccount.lock.withLock { RecordCacheAccount(privateContext, privateKeys, database).clear() } }
      finally { database.close() }
    }
  }

  @Test fun expiredCursorRestartsOnceWithoutDownloadingSavedDetailsAgain() = runBlocking {
    val calls = mutableListOf<String>()
    repository(MockEngine { request ->
      val cursor = request.url.parameters["cursor"]
      calls.add(cursor ?: "first")
      if (cursor != null) respond("""{"error":{"code":"CURSOR_INVALID"}}""",
        HttpStatusCode.BadRequest, headersOf(HttpHeaders.ContentType, "application/json"))
      else respond(list(cursor = "new.signature"), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      // Detail already exists, so the restart will enumerate it but must not request it again.
      repository(MockEngine { respond(detail(), headers = headersOf(HttpHeaders.ContentType, "application/json")) })
        .use { it.record(token, accountId, recordId) }
      records.saveSyncCheckpoint(accountId, RecordSyncCheckpoint(cursor = "old.signature"))
      try { records.synchronize(token, accountId); fail("a second invalid cursor must stop sync") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.CURSOR_INVALID, error.failure) }
      assertEquals(listOf("old.signature", "first", "new.signature"), calls)
      assertEquals("架空の結論", records.cachedRecord(accountId, recordId)?.decision)
    }
  }

  @Test fun cyclicCursorsStopAndMissingDetailsDoNotBlockRemainingRecords() = runBlocking {
    var calls = 0
    repository(MockEngine {
      val next = listOf("a.signature", "b.signature", "a.signature")[calls++]
      respond(list(emptyList(), next), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      try { records.synchronize(token, accountId); fail("cursor cycle must not loop forever") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.INVALID_RESPONSE, error.failure) }
      assertEquals(3, calls)
    }
    val remaining = "c".repeat(43)
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      if (id == recordId) respond("", HttpStatusCode.NotFound)
      else respond(if (id == "records") list(listOf(recordId, remaining)) else detail(id),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      records.saveSyncCheckpoint(accountId, RecordSyncCheckpoint())
      records.synchronize(token, accountId)
      assertNull(records.cachedRecord(accountId, recordId))
      assertEquals("架空の結論", records.cachedRecord(accountId, remaining)?.decision)
      assertEquals(true, records.syncCheckpoint(accountId)?.complete)
    }
  }

  @Test fun authorizationLossDuringSyncCannotSaveTheDelayedDetailOrAdvanceProgress() = runBlocking {
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      if (id != "records") activeAccountId = ""
      respond(if (id == "records") list() else detail(),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      try { records.synchronize(token, accountId); fail("lost authorization must stop sync") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure) }
      activeAccountId = accountId
      assertNull(records.cachedRecord(accountId, recordId))
      assertEquals(listOf(recordId), records.syncCheckpoint(accountId)?.pendingIds)
    }
  }

  @Test fun failedSaveNeverReturnsOnlinePlaintext() = runBlocking {
    activeAccountId = "invalid account"
    repository(MockEngine { request ->
      respond(if (request.url.encodedPath.endsWith("/$recordId")) detail() else list(),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { repository ->
      for (request in listOf<suspend () -> Any>(
        { repository.recentRecords(token, "invalid account", null) },
        { repository.record(token, "invalid account", recordId) },
      )) {
        try {
          request()
          fail("online data must not be returned after a failed cache save")
        } catch (error: RecordReadException) {
          assertEquals(RecordReadFailure.STORAGE_UNAVAILABLE, error.failure)
        }
      }
    }
  }

  @Test fun authorizationExpiryLocksButSameAccountReauthenticationKeepsTheCiphertext() = runBlocking {
    repository(MockEngine {
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { it.recentRecords(token, accountId, null) }
    activeAccountId = "" // The session gate is closed, but no deletion is requested.
    repository(MockEngine { error("unexpected_network_request") }).use { cached ->
      try {
        cached.cachedListEntry(accountId, recordId)
        fail("expired authorization must lock the cache")
      } catch (error: RecordReadException) {
        assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure)
      }
      activeAccountId = accountId
      assertEquals("架空の議題", cached.cachedListEntry(accountId, recordId)?.questionPreview)
    }
  }

  @Test fun explicitLogoutInvalidatesPrivateKeyAndErasesAllRowsAndOwnerMarker() = runBlocking {
    repository(MockEngine {
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use {
      it.recentRecords(token, accountId, null)
      it.saveSyncCheckpoint(accountId, RecordSyncCheckpoint())
    }
    val unrelated = File(privateDirectory, "unrelated").apply { writeText("keep") }
    val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    try {
      RecordCacheAccount.lock.withLock {
        RecordCacheAccount(privateContext, privateKeys, database).clear()
      }
      assertNull(privateKeys.read(accountId))
      assertNull(EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
        .load(accountId, recordId, CachedRecordPart.LIST))
      assertEquals(listOf(unrelated.name), privateDirectory.listFiles()!!.map { it.name })
    } finally { database.close() }
    // The same account can start a genuinely empty cache after an explicit logout.
    repository(MockEngine {
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { assertEquals(1, it.recentRecords(token, accountId, null).items.size) }
  }

  @Test fun interruptedLogoutResumesDeletionBeforeTheSameAccountCanReadAgain() = runBlocking {
    repository(MockEngine {
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { it.recentRecords(token, accountId, null) }
    val closed = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    closed.records().get("unused", recordId, "list") // Open, then force a real deletion failure.
    closed.close()
    RecordCacheAccount.lock.withLock {
      try {
        RecordCacheAccount(privateContext, privateKeys, closed).clear()
        fail("closed database must not report successful logout cleanup")
      } catch (_: RecordCacheException) { }
    }
    assertNull(privateKeys.read(accountId))
    assertEquals(true, File(privateDirectory, "records-cache-delete.v1").exists())
    val reopened = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    try {
      RecordCacheAccount.lock.withLock {
        RecordCacheAccount(privateContext, privateKeys, reopened).activate(accountId)
      }
      assertNull(EncryptedRecordStore(reopened, RecordDataKeyProtector(privateKeys))
        .load(accountId, recordId, CachedRecordPart.LIST))
      assertFalse(File(privateDirectory, "records-cache-delete.v1").exists())
    } finally { reopened.close() }
  }

  private fun repository(engine: MockEngine): RecordsRepository {
    val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    return RecordsRepository(RecordsReadClient(engine),
      EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys)), database,
      RecordCacheAccount(privateContext, privateKeys, database), { it == activeAccountId })
  }

  private fun list(ids: List<String> = listOf(recordId), cursor: String? = null): String =
    """{"schemaVersion":1,"items":[${ids.joinToString { listItem(it) }}],"nextCursor":${cursor?.let { "\"$it\"" } ?: "null"}}"""

  private fun listItem(id: String): String = """{"schemaVersion":1,"recordId":"$id",
    "questionPreview":"架空の議題","completedAt":"2026-09-24T00:00:00Z",
    "requester":{"displayName":"架空の依頼者","avatar":{"kind":"placeholder",
      "url":null,"alt":"架空の依頼者","fallbackVariant":"cyan"}},
    "participants":[{"slot":"participant-a","displayName":"アロナ"},
      {"slot":"participant-b","displayName":"プラナ"},
      {"slot":"participant-c","displayName":"安倍晋三AI"}],
    "result":{"winner":"participant-a"}}"""

  private fun detail(id: String = recordId): String = """{"schemaVersion":2,"recordId":"$id","question":"架空の議題",
    "participants":[{"slot":"participant-a","displayName":"アロナ"},
      {"slot":"participant-b","displayName":"プラナ"},
      {"slot":"participant-c","displayName":"安倍晋三AI"}],
    "initialOpinions":[{"participant":"participant-a","summary":"要約A","proposal":"案A"},
      {"participant":"participant-b","summary":"要約B","proposal":"案B"},
      {"participant":"participant-c","summary":"要約C","proposal":"案C"}],
    "finalProposals":[{"participant":"participant-a","title":"最終案A","proposal":"決定案A"},
      {"participant":"participant-b","title":"最終案B","proposal":"決定案B"},
      {"participant":"participant-c","title":"最終案C","proposal":"決定案C"}],
    "votes":[{"voter":"participant-a","candidate":"participant-b","reason":"理由A"},
      {"voter":"participant-b","candidate":"participant-a","reason":"理由B"},
      {"voter":"participant-c","candidate":"participant-a","reason":"理由C"}],
    "result":{"winner":"participant-a","voteCounts":[
      {"participant":"participant-a","count":2},{"participant":"participant-b","count":1},
      {"participant":"participant-c","count":0}],"tieBreakApplied":false},
    "finalDecision":{"winner":"participant-a","decision":"架空の結論","actions":[],"caveats":[]},
    "affection":null}"""
}
