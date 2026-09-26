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
import io.ktor.http.headersOf
import java.io.File
import java.nio.file.Files
import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
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
    }).use { it.recentRecords(token, accountId, null) }
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

  private fun list(): String = """{"schemaVersion":1,"items":[{"schemaVersion":1,"recordId":"$recordId",
    "questionPreview":"架空の議題","completedAt":"2026-09-24T00:00:00Z",
    "requester":{"displayName":"架空の依頼者","avatar":{"kind":"placeholder",
      "url":null,"alt":"架空の依頼者","fallbackVariant":"cyan"}},
    "participants":[{"slot":"participant-a","displayName":"アロナ"},
      {"slot":"participant-b","displayName":"プラナ"},
      {"slot":"participant-c","displayName":"安倍晋三AI"}],
    "result":{"winner":"participant-a"}}],"nextCursor":null}"""

  private fun detail(): String = """{"schemaVersion":2,"recordId":"$recordId","question":"架空の議題",
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
