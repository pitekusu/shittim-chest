package dev.pitekusu.shittim.records.storage

import android.content.Context
import android.content.ContextWrapper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.RecordAttachment
import dev.pitekusu.shittim.records.RecordImageException
import dev.pitekusu.shittim.records.attachmentDigest
import java.io.File
import java.nio.file.Files
import java.util.UUID
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class EncryptedRecordImageStoreTest {
  private lateinit var directory: File
  private lateinit var context: Context
  private lateinit var keys: KeystorePrivateKeyStore
  private val account = "synthetic_account"
  private val imageId = "11111111-2222-4333-8444-abcdefabcdef"
  @Before fun setup() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "encrypted-image-test-").toFile()
    val testPackage = "${app.packageName}.image.${UUID.randomUUID()}"
    context = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir() = directory
      override fun getPackageName() = testPackage
    }
    keys = KeystorePrivateKeyStore(context)
  }
  @After fun cleanup() { keys.clear(); directory.deleteRecursively() }

  @Test fun originalsLargerThanRoomLimitSurviveReopenAndNeverBecomePlaintextFiles() = runBlocking {
    val bytes = ByteArray(2 * 1024 * 1024) { (it % 251).toByte() }
    val image = RecordAttachment(imageId, "image/png", bytes.size, attachmentDigest(bytes), 1024, 1024)
    EncryptedRecordImageStore(context).save(account, "synthetic_record", image, bytes)
    val reopened = EncryptedRecordImageStore(context)
    assertArrayEquals(bytes, reopened.read(account, "synthetic_record", image))
    val file = File(directory, "record-images-v1").walkTopDown().first { it.isFile }
    assertFalse(file.readBytes().contentEquals(bytes))
    assertFalse(file.name.contains(account))
    assertNull(reopened.read("different_account", "synthetic_record", image))
    assertNull(reopened.read(account, "different_record", image))
    reopened.retain(account, "synthetic_record", emptyList())
    assertNull(reopened.read(account, "synthetic_record", image))
  }

  @Test fun tamperingAndKeyLossNeverReturnUnverifiedBytes() = runBlocking {
    val bytes = ByteArray(1024) { 42 }
    val image = RecordAttachment(imageId, "image/png", bytes.size, attachmentDigest(bytes), 32, 32)
    val store = EncryptedRecordImageStore(context)
    store.save(account, "debate-draft", image, bytes)
    val file = File(directory, "record-images-v1").walkTopDown().first { it.isFile }
    file.writeBytes(file.readBytes().also { it[it.lastIndex] = (it.last().toInt() xor 1).toByte() })
    failure { store.read(account, "debate-draft", image) }
    store.save(account, "debate-draft", image, bytes)
    keys.clear()
    failure { store.read(account, "debate-draft", image) }
  }

  @Test fun lowStorageDoesNotReplaceAnExistingOriginalAndClearPreservesUnrelatedFiles() = runBlocking {
    val bytes = ByteArray(128) { 7 }
    val image = RecordAttachment(imageId, "image/png", bytes.size, attachmentDigest(bytes), 16, 8)
    val store = EncryptedRecordImageStore(context)
    store.save(account, "debate-draft", image, bytes)
    failure { EncryptedRecordImageStore(context, availableBytes = { 64L * 1024 * 1024 })
      .save(account, "debate-draft", image, bytes) }
    assertArrayEquals(bytes, store.read(account, "debate-draft", image))
    val unrelated = File(directory, "unrelated").apply { writeText("keep") }
    EncryptedRecordImageStore.clear(context)
    assertFalse(File(directory, "record-images-v1").exists())
    assertEquals("keep", unrelated.readText())
  }
  private suspend fun failure(block: suspend () -> Any?) {
    try { block(); fail("expected encrypted image failure") }
    catch (error: RecordImageException) { assertNull(error.cause); assertTrue(error.message!!.startsWith("record_image_")) }
  }
}
