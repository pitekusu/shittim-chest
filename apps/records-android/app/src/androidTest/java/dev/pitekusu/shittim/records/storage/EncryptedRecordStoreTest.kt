package dev.pitekusu.shittim.records.storage

import android.content.Context
import android.content.ContextWrapper
import android.util.Base64
import androidx.room3.Room
import androidx.sqlite.driver.AndroidSQLiteDriver
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.nio.file.Files
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class EncryptedRecordStoreTest {
  private lateinit var app: Context
  private lateinit var directory: File
  private lateinit var databaseName: String
  private lateinit var database: EncryptedRecordsDatabase
  private lateinit var privateKeys: KeystorePrivateKeyStore
  private lateinit var store: EncryptedRecordStore
  private val account = "synthetic_account"
  private val record = "synthetic_record"

  @Before
  fun setUp() {
    app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "encrypted-room-test-").toFile()
    val packageName = "${app.packageName}.test.${UUID.randomUUID()}"
    val privateKeyContext = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = packageName
    }
    privateKeys = KeystorePrivateKeyStore(privateKeyContext)
    databaseName = "encrypted-records-test-${UUID.randomUUID()}.db"
    database = openDatabase()
    store = EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
  }

  @After
  fun tearDown() {
    database.close()
    privateKeys.clear()
    app.deleteDatabase(databaseName)
    directory.deleteRecursively()
  }

  @Test
  fun listAndDetailSurviveReopenWithoutPlaintextInSqliteFiles() = runBlocking {
    val list = "PRIVATE_REQUESTER_NAME and list preview".toByteArray()
    val detail = "PRIVATE_QUESTION_TEXT and debate conclusion".toByteArray()
    store.save(account, record, CachedRecordPart.LIST, list)
    store.save(account, record, CachedRecordPart.DETAIL, detail)
    assertArrayEquals(list, store.load(account, record, CachedRecordPart.LIST))
    assertArrayEquals(detail, store.load(account, record, CachedRecordPart.DETAIL))
    assertNull(store.load("different_account", record, CachedRecordPart.LIST))
    database.close()
    val files = app.getDatabasePath(databaseName).parentFile!!.listFiles()!!
      .filter { it.name.startsWith(databaseName) && it.isFile }
    assertFalse(files.isEmpty())
    for (file in files) {
      val bytes = file.readBytes().toString(Charsets.ISO_8859_1)
      assertFalse(bytes.contains("PRIVATE_REQUESTER_NAME"))
      assertFalse(bytes.contains("PRIVATE_QUESTION_TEXT"))
      assertFalse(bytes.contains(account))
    }
    database = openDatabase()
    store = EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
    assertArrayEquals(list, store.load(account, record, CachedRecordPart.LIST))
    assertArrayEquals(detail, store.load(account, record, CachedRecordPart.DETAIL))
  }

  @Test
  fun tamperingOrLosingPrivateKeyFailsClosedAndAccountDeletionIsScoped() = runBlocking {
    val payload = "synthetic encrypted record".toByteArray()
    store.save(account, record, CachedRecordPart.DETAIL, payload)
    val row = database.records().get(accountKey(), record, CachedRecordPart.DETAIL.code)!!
    val tampered = row.encryptedPayload.copyOf().apply {
      this[lastIndex] = (this[lastIndex].toInt() xor 1).toByte()
    }
    database.records().put(EncryptedRecordRow(row.accountKey, row.recordId, row.part, row.wrappedKey, tampered))
    assertCacheFailure { store.load(account, record, CachedRecordPart.DETAIL) }
    store.save(account, record, CachedRecordPart.DETAIL, payload)
    privateKeys.clear()
    assertCacheFailure { store.load(account, record, CachedRecordPart.DETAIL) }
    store.deleteAccount(account)
    assertNull(store.load(account, record, CachedRecordPart.DETAIL))
  }

  @Test
  fun invalidInputsNeverPersistARecord() = runBlocking {
    assertCacheFailure { store.save(account, record, CachedRecordPart.LIST, byteArrayOf()) }
    assertCacheFailure { store.save(account, "invalid/record", CachedRecordPart.LIST, byteArrayOf(1)) }
    assertNull(database.records().get(accountKey(), record, CachedRecordPart.LIST.code))
    assertNull(privateKeys.read(account))
  }

  @Test
  fun largestAcceptedPayloadCanBeReadAndOversizeDoesNotReplaceIt() = runBlocking {
    val payload = ByteArray(1024 * 1024) { (it % 251).toByte() }
    store.save(account, record, CachedRecordPart.DETAIL, payload)
    assertArrayEquals(payload, store.load(account, record, CachedRecordPart.DETAIL))
    assertCacheFailure { store.save(account, record, CachedRecordPart.DETAIL, ByteArray(payload.size + 1)) }
    assertArrayEquals(payload, store.load(account, record, CachedRecordPart.DETAIL))
  }

  private fun openDatabase(): EncryptedRecordsDatabase =
    Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver())
      .build()

  private fun accountKey(): String = Base64.encodeToString(
    MessageDigest.getInstance("SHA-256")
      .digest("shittim-records|account-index|v1|$account".toByteArray(Charsets.US_ASCII)),
    Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP,
  )

  private suspend fun assertCacheFailure(operation: suspend () -> Any?) {
    val error = try {
      operation()
      fail("expected record cache failure")
      return
    } catch (error: RecordCacheException) {
      error
    }
    assertEquals("record_cache_unavailable", error.message)
    assertNull(error.cause)
  }
}
