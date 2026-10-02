package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import androidx.room3.Room
import androidx.sqlite.driver.AndroidSQLiteDriver
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordRow
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import io.ktor.client.engine.mock.MockEngine
import java.io.File
import java.nio.file.Files
import java.time.Instant
import java.util.Base64
import java.util.UUID
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordsRepositoryCacheTest {
  private val accountId = "u".repeat(43)
  private val firstId = "a".repeat(43)
  private val secondId = "b".repeat(43)
  private lateinit var app: Context
  private lateinit var privateContext: Context
  private lateinit var directory: File
  private lateinit var databaseName: String
  private lateinit var database: EncryptedRecordsDatabase
  private lateinit var privateKeys: KeystorePrivateKeyStore
  private lateinit var store: EncryptedRecordStore
  private var activeAccountId = accountId

  @Before fun setUp() = runBlocking {
    app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "repo-cache-test-").toFile()
    privateContext = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = "${app.packageName}.test.${UUID.randomUUID()}"
    }
    privateKeys = KeystorePrivateKeyStore(privateContext)
    databaseName = "repo-cache-test-${UUID.randomUUID()}.db"
    database = openDatabase()
    store = EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
    activeAccountId = accountId
    RecordCacheAccount.lock.withLock {
      RecordCacheAccount(privateContext, privateKeys, database).activate(accountId)
    }
  }

  @After fun tearDown() {
    database.close()
    privateKeys.clear()
    app.deleteDatabase(databaseName)
    directory.deleteRecursively()
  }

  @Test fun completeIdsRequireBothPartsWithoutReadingTheirPlaintextAndHonorAuthorization() = runBlocking {
    saveList(entry(firstId))
    RecordCacheAccount.lock.withLock {
      store.save(accountId, secondId, CachedRecordPart.DETAIL, byteArrayOf(1))
    }
    repository().use { records ->
      assertEquals(emptySet<String>(), records.cachedCompleteRecordIds(accountId))
      RecordCacheAccount.lock.withLock {
        // A presence check must not parse or unwrap the body at all.
        store.save(accountId, firstId, CachedRecordPart.DETAIL, byteArrayOf(1))
      }
      assertEquals(setOf(firstId), records.cachedCompleteRecordIds(accountId))
      activeAccountId = ""
      assertReadFailure(RecordReadFailure.AUTH_REQUIRED) { records.cachedCompleteRecordIds(accountId) }
    }
  }

  @Test fun aSeparateWriterUpdatesAndDeletesRowsWhileAnOfflineReaderKeepsItsMemo() = runBlocking {
    saveList(entry(firstId))
    saveList(entry(secondId, "変更しない架空の議題"))
    repository().use { records ->
      assertEquals(2, records.cachedRecords(accountId).size)
      assertEquals(2, records.cachedRecords(accountId).size)
      saveList(entry(firstId, "更新した架空の議題"))
      val refreshed = records.cachedRecords(accountId).associateBy { it.recordId }
      assertEquals("更新した架空の議題", refreshed[firstId]?.questionPreview)
      assertEquals("変更しない架空の議題", refreshed[secondId]?.questionPreview)
      assertNull(refreshed[firstId]?.requesterAvatar?.url)
      RecordCacheAccount.lock.withLock { store.deleteRecords(accountId, setOf(firstId)) }
      assertEquals(listOf(secondId), records.cachedRecords(accountId).map { it.recordId })
    }
  }

  @Test fun memoizedIconsHaveIndependentUiOwnershipAndLogoutCannotReuseTheOldList() = runBlocking {
    val url = "https://fixture.s3.ap-northeast-1.amazonaws.com/requesters/fake/avatar.png?signature=synthetic"
    val revision = "i".repeat(43)
    val avatarEntry = entry(firstId, avatar = RecordAvatar(url, "cyan", revision = revision))
    val source = "avatar-${avatarSource(url)}-$revision"
    val original = byteArrayOf(1, 2, 3, 4)
    saveList(avatarEntry)
    RecordCacheAccount.lock.withLock {
      val encoded = Base64.getEncoder().encodeToString(original)
      store.save(accountId, source, CachedRecordPart.AVATAR,
        """{"source":"$source","data":"$encoded"}""".toByteArray())
    }
    lateinit var returned: ByteArray
    repository().use { records ->
      val first = records.cachedRecords(accountId).single().requesterAvatar.bytes!!
      first[0] = 99
      val second = records.cachedRecords(accountId).single().requesterAvatar.bytes!!
      assertArrayEquals(original, second)
      RecordCacheAccount.lock.withLock {
        RecordCacheAccount(privateContext, privateKeys, database).clear()
      }
      assertReadFailure(RecordReadFailure.STORAGE_UNAVAILABLE) { records.cachedRecords(accountId) }
      assertArrayEquals(original, second)
      RecordCacheAccount.lock.withLock {
        RecordCacheAccount(privateContext, privateKeys, database).activate(accountId)
      }
      assertEquals(emptyList<RecordListEntry>(), records.cachedRecords(accountId))
      saveList(entry(secondId, "再ログイン後の架空の議題"))
      assertEquals(secondId, records.cachedRecords(accountId).single().recordId)
      returned = second
    }
    assertArrayEquals(original, returned)
  }

  @Test fun changedCiphertextAndChangedWrappedKeyNeverReturnMemoizedPlaintext() = runBlocking {
    saveList(entry(firstId))
    repository().use { records ->
      assertEquals(firstId, records.cachedRecords(accountId).single().recordId)
      for (changePayload in listOf(true, false)) {
        val row = store.rows(accountId, CachedRecordPart.LIST).single()
        val wrapped = row.wrappedKey.copyOf()
        val payload = row.encryptedPayload.copyOf()
        val changed = if (changePayload) payload else wrapped
        changed[changed.lastIndex] = (changed.last().toInt() xor 1).toByte()
        RecordCacheAccount.lock.withLock {
          database.records().put(EncryptedRecordRow(row.accountKey, row.recordId, row.part, wrapped, payload))
        }
        assertReadFailure(RecordReadFailure.STORAGE_UNAVAILABLE) { records.cachedRecords(accountId) }
        saveList(entry(firstId))
        assertEquals(firstId, records.cachedRecords(accountId).single().recordId)
      }
    }
  }

  private fun entry(id: String, question: String = "架空の議題",
    avatar: RecordAvatar = RecordAvatar(null, "cyan")): RecordListEntry =
    RecordListEntry(id, question, "架空の依頼者", avatar, Instant.parse("2026-09-24T00:00:00Z"),
      "アロナ", "participant-a")

  private suspend fun saveList(entry: RecordListEntry) = RecordCacheAccount.lock.withLock {
    val encoded = Json.encodeToString(entry)
    store.save(accountId, entry.recordId, CachedRecordPart.LIST,
      """{"schemaVersion":1,"entry":$encoded}""".toByteArray())
  }

  private fun openDatabase(): EncryptedRecordsDatabase =
    Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()

  private fun repository(): RecordsRepository {
    val readerDatabase = openDatabase()
    return RecordsRepository(RecordsReadClient(MockEngine { error("offline_must_not_request_network") }),
      EncryptedRecordStore(readerDatabase, RecordDataKeyProtector(privateKeys)), readerDatabase,
      RecordCacheAccount(privateContext, privateKeys, readerDatabase), { it == activeAccountId }, activateOwner = false)
  }

  private suspend fun assertReadFailure(expected: RecordReadFailure, operation: suspend () -> Any?) {
    try {
      operation()
      fail("expected record read failure")
    } catch (error: RecordReadException) { assertEquals(expected, error.failure) }
  }
}
