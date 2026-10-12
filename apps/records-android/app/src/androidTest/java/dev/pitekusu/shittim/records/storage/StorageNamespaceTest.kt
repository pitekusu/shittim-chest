package dev.pitekusu.shittim.records.storage

import android.content.Context
import android.content.ContextWrapper
import android.graphics.Bitmap
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.DEMO_ACCOUNT_ID
import dev.pitekusu.shittim.records.DebateRequestException
import dev.pitekusu.shittim.records.DebateWorkspace
import dev.pitekusu.shittim.records.DebateWorkspaceStore
import dev.pitekusu.shittim.records.RecordAttachment
import dev.pitekusu.shittim.records.RecordImageException
import dev.pitekusu.shittim.records.RecordImageFailure
import dev.pitekusu.shittim.records.attachmentDigest
import java.io.ByteArrayOutputStream
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
class StorageNamespaceTest {
  private lateinit var directory: File
  private lateinit var context: Context
  private val account = DEMO_ACCOUNT_ID // Deliberately identical: isolation must not depend on differing IDs.
  private val record = "synthetic_record"

  @Before fun setup() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "storage-namespace-test-").toFile()
    val testPackage = "${app.packageName}.namespace.${UUID.randomUUID()}"
    context = object : ContextWrapper(app) {
      override fun getApplicationContext(): Context = this
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = testPackage
      override fun getDatabasePath(name: String): File = File(directory, name)
    }
  }

  @After fun cleanup() {
    StorageNamespace.entries.forEach { KeystorePrivateKeyStore(context, it).clear() }
    directory.deleteRecursively()
  }

  @Test fun sameOwnerCannotReadAcrossNamespacesAndCleanupPreservesTheOtherNamespace() = runBlocking {
    val production = EncryptedRecordsDatabase.open(context)
    val demo = EncryptedRecordsDatabase.open(context, StorageNamespace.DEMO)
    try {
      val productionKeys = KeystorePrivateKeyStore(context)
      val demoKeys = KeystorePrivateKeyStore(context, StorageNamespace.DEMO)
      val productionCache = EncryptedRecordStore(production, RecordDataKeyProtector(productionKeys))
      val demoCache = EncryptedRecordStore(demo, RecordDataKeyProtector(demoKeys))
      val productionOwner = RecordCacheAccount(context, productionKeys, production)
      val demoOwner = RecordCacheAccount(context, demoKeys, demo, StorageNamespace.DEMO)
      val productionImages = EncryptedRecordImageStore(context)
      val demoImages = EncryptedRecordImageStore(context, namespace = StorageNamespace.DEMO)
      val bytes = png()
      val image = RecordAttachment(UUID.randomUUID().toString(), "image/png", bytes.size,
        attachmentDigest(bytes), 8, 8)
      productionOwner.activate(account)
      productionCache.save(account, record, CachedRecordPart.DETAIL, "production only".encodeToByteArray())
      productionImages.save(account, record, image, bytes)
      demoOwner.activate(account)
      assertNull(demoCache.load(account, record, CachedRecordPart.DETAIL))
      assertNull(demoImages.read(account, record, image))
      demoCache.save(account, record, CachedRecordPart.DETAIL, "demo only".encodeToByteArray())
      demoImages.save(account, record, image, bytes)
      assertEquals("production only", productionCache.load(account, record, CachedRecordPart.DETAIL)!!.decodeToString())
      assertEquals("demo only", demoCache.load(account, record, CachedRecordPart.DETAIL)!!.decodeToString())
      assertArrayEquals(bytes, demoImages.read(account, record, image))
      assertTrue(File(directory, "records-private-key.v1").isFile)
      assertTrue(File(directory, "review-demo-records-private-key.v1").isFile)
      // Even deliberately transplanted ciphertext cannot be unwrapped using the other namespace's key.
      val productionRow = productionCache.rows(account, CachedRecordPart.DETAIL).single()
      try { demoCache.decryptRow(account, productionRow, CachedRecordPart.DETAIL); fail("cross-namespace ciphertext") }
      catch (_: RecordCacheException) { }
      RecordCacheAccount.clear(context, StorageNamespace.DEMO)
      assertNull(demoCache.load(account, record, CachedRecordPart.DETAIL))
      assertNull(demoImages.read(account, record, image))
      assertEquals("production only", productionCache.load(account, record, CachedRecordPart.DETAIL)!!.decodeToString())
      assertArrayEquals(bytes, productionImages.read(account, record, image))
      productionOwner.requireOwner(account)
      demoOwner.activate(account)
      demoCache.save(account, record, CachedRecordPart.DETAIL, "new demo".encodeToByteArray())
      demoImages.save(account, record, image, bytes)
      RecordCacheAccount.clear(context)
      assertEquals("new demo", demoCache.load(account, record, CachedRecordPart.DETAIL)!!.decodeToString())
      assertArrayEquals(bytes, demoImages.read(account, record, image))
      demoOwner.requireOwner(account)
    } finally { production.close(); demo.close() }
  }

  @Test fun demoWorkspaceAndImportedImagesSurviveReopenWithoutChangingProductionDraft() = runBlocking {
    RecordCacheAccount.activate(context, account)
    RecordCacheAccount.activate(context, account, StorageNamespace.DEMO)
    val bytes = png()
    DebateWorkspaceStore.open(context, account, { true }).use { production ->
      production.save(account, DebateWorkspace(draft = "通常の架空下書き"))
    }
    var descriptor: dev.pitekusu.shittim.records.DraftAttachment? = null
    DebateWorkspaceStore.open(context, account, { true }, StorageNamespace.DEMO).use { demo ->
      val image = demo.importBytes(account, bytes)
      descriptor = image
      assertEquals(attachmentDigest(bytes), image.sha256)
      assertArrayEquals(bytes, demo.image(account, image))
      demo.save(account, DebateWorkspace(draft = "デモの架空下書き", attachments = listOf(image)))
      try { demo.load("x".repeat(43)); fail("a bound workspace must reject another owner") }
      catch (_: DebateRequestException) { }
      try { demo.importBytes(account, byteArrayOf(0, 1, 2)); fail("invalid sample image") }
      catch (error: RecordImageException) { assertEquals(RecordImageFailure.INVALID, error.failure) }
    }
    DebateWorkspaceStore.open(context, account, { true }, StorageNamespace.DEMO).use { reopened ->
      assertEquals("デモの架空下書き", reopened.load(account).draft)
      assertArrayEquals(bytes, reopened.image(account, requireNotNull(descriptor)))
    }
    DebateWorkspaceStore.open(context, account, { true }).use { production ->
      assertEquals("通常の架空下書き", production.load(account).draft)
      assertTrue(production.load(account).attachments.isEmpty())
    }
  }

  private fun png(): ByteArray {
    val bitmap = Bitmap.createBitmap(8, 8, Bitmap.Config.ARGB_8888)
    return try {
      bitmap.eraseColor(0xff00aacc.toInt())
      ByteArrayOutputStream().use { output ->
        check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output))
        output.toByteArray()
      }
    } finally { bitmap.recycle() }
  }
}
