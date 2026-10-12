package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import dev.pitekusu.shittim.records.storage.StorageNamespace
import java.io.File
import java.nio.file.Files
import java.time.Instant
import java.util.UUID
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DemoSandboxTest {
  private lateinit var context: Context
  private lateinit var directory: File
  private lateinit var mode: ReviewDemoModeStore
  private lateinit var entry: String
  private var now = Instant.parse("2026-10-12T01:00:00Z")
  private val sandboxes = mutableListOf<DemoSandbox>()

  @Before fun prepare() = runBlocking {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "demo-sandbox-test-").toFile()
    val testPackage = "${app.packageName}.sandbox.${UUID.randomUUID()}"
    context = object : ContextWrapper(app) {
      override fun getApplicationContext(): Context = this
      override fun getNoBackupFilesDir(): File = directory
      override fun getDatabasePath(name: String): File = File(directory, name)
      override fun getPackageName(): String = testPackage
    }
    mode = ReviewDemoModeStore(context)
    enterDemo()
  }

  @After fun cleanup() = runBlocking {
    sandboxes.forEach(DemoSandbox::close)
    RecordCacheAccount.clear(context, StorageNamespace.DEMO)
    RecordCacheAccount.clear(context, StorageNamespace.PRODUCTION)
    directory.deleteRecursively()
    Unit
  }

  private suspend fun enterDemo() {
    entry = UUID.randomUUID().toString()
    mode.write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    RecordCacheAccount.activate(context, DEMO_ACCOUNT_ID, StorageNamespace.DEMO)
  }

  private fun backend(onPublished: (String) -> Unit = {}): DemoSandbox =
    DemoSandbox(context, entry, onPublished, { now }).also(sandboxes::add)

  @Test fun authoredRecordsUseTheExistingSearchFiltersDetailAndValidatedImageBytes() = runBlocking {
    val demo = backend()
    val entries = demo.cachedRecords(DEMO_ACCOUNT_ID)
    assertEquals(6, entries.size)
    assertEquals(3, entries.map { it.winnerSlot }.toSet().size)
    val selected = demo.queryCachedRecords(DEMO_ACCOUNT_ID, entries,
      RecordListQuery(text = "架空 採点", winner = RecordWinner.Arona))
    assertEquals(2, selected.size)
    val first = entries.first()
    val detail = requireNotNull(demo.cachedRecord(DEMO_ACCOUNT_ID, first.recordId))
    assertEquals(3, detail.opinions.size)
    assertEquals(3, detail.voting?.votes?.size)
    assertEquals(RecordAffectionReasonStatus.AVAILABLE, detail.affection?.changes?.first()?.reasonStatus)
    val reasons = entries.flatMap { entry ->
      requireNotNull(demo.cachedRecord(DEMO_ACCOUNT_ID, entry.recordId)).affection?.changes.orEmpty()
    }
    assertEquals(setOf(RecordAffectionReasonStatus.AVAILABLE, RecordAffectionReasonStatus.NOT_RECORDED,
      RecordAffectionReasonStatus.UNAVAILABLE), reasons.map { it.reasonStatus }.toSet())
    assertTrue(reasons.filter { it.reasonStatus == RecordAffectionReasonStatus.AVAILABLE }.all { !it.reason.isNullOrBlank() })
    assertTrue(reasons.filter { it.reasonStatus != RecordAffectionReasonStatus.AVAILABLE }.all { it.reason == null })
    assertTrue(detail.decision.contains("AIが分析した結果ではありません"))
    val image = detail.attachments.single()
    assertNull(image.url)
    val bytes = requireNotNull(demo.cachedAttachment(DEMO_ACCOUNT_ID, first.recordId, image))
    try { assertEquals(640 to 480, validateAttachmentBytes(bytes, image)) }
    finally { bytes.fill(0) }
    val multiple = entries.first { entry -> DemoFixtures.records.first { it.entry.recordId == entry.recordId }.preview.attachments.size == 3 }
    val images = requireNotNull(demo.cachedRecord(DEMO_ACCOUNT_ID, multiple.recordId)).attachments
    assertEquals(3, images.map { it.sha256 }.toSet().size)
    for (descriptor in images) {
      val original = requireNotNull(demo.cachedAttachment(DEMO_ACCOUNT_ID, multiple.recordId, descriptor))
      try { validateAttachmentBytes(original, descriptor) } finally { original.fill(0) }
    }
    assertTrue(demo.list().items.isEmpty())
  }

  @Test fun restartAndRepeatedSubmissionKeepOneRequestAndItsImageSurvivesANewDraft() = runBlocking {
    val published = mutableListOf<String>()
    var demo = backend(published::add)
    val question = "架空の再起動テスト：海辺で工作会を開きます。"
    val image = demo.importImage(DEMO_SAMPLE_IMAGE_URI)
    demo.save(DebateWorkspace(draft = question, attachments = listOf(image)))
    val id = UUID.randomUUID().toString()
    val accepted = demo.submit(id, question, listOf(image))
    demo.save(DebateWorkspace(draft = question, requestId = id, frozenQuestion = question,
      receipt = accepted, attachments = listOf(image)))
    assertEquals("accepted", accepted.status)
    now = now.plusSeconds(2)
    assertEquals("queued", demo.find(id)?.status)
    now = now.plusSeconds(2)
    assertEquals("starting", demo.find(id)?.status)
    demo.close()
    demo = backend(published::add)
    assertEquals(id, demo.load().requestId)
    now = now.plusSeconds(2)
    assertEquals("running", demo.find(id)?.status)
    now = now.minusSeconds(20)
    assertEquals("running", demo.find(id)?.status)
    now = now.plusSeconds(2)
    assertEquals("publishing", demo.find(id)?.status)
    now = now.plusSeconds(2)
    val result = requireNotNull(demo.find(id))
    assertEquals("published", result.status)
    result.validate()
    assertEquals(result.recordId, demo.submit(id, question, listOf(image)).recordId)
    assertEquals(1, demo.list().items.size)
    assertEquals(listOf(result.recordId), published)
    demo.save(DebateWorkspace())
    assertTrue(demo.load().attachments.isEmpty())
    val recordId = requireNotNull(result.recordId)
    val detail = requireNotNull(demo.cachedRecord(DEMO_ACCOUNT_ID, recordId))
    val stored = requireNotNull(demo.cachedAttachment(DEMO_ACCOUNT_ID, recordId, detail.attachments.single()))
    val sample = DemoFixtures.sampleImageBytes()
    try { assertArrayEquals(sample, stored) } finally { sample.fill(0); stored.fill(0) }
    val search = demo.queryCachedRecords(DEMO_ACCOUNT_ID, demo.cachedRecords(DEMO_ACCOUNT_ID),
      RecordListQuery(text = "海辺", requesterName = "デモ利用者"))
    assertEquals(listOf(recordId), search.map { it.recordId })
    assertEquals(7, demo.cachedRecords(DEMO_ACCOUNT_ID).size)
  }

  @Test fun restartedRequestWithAFutureCreationTimeStillFinishesAfterTenLocalSeconds() = runBlocking {
    var demo = backend()
    val id = UUID.randomUUID().toString()
    val accepted = demo.submit(id, "架空の時計変更テスト")
    demo.close()
    now = now.minusSeconds(365L * 86_400)
    demo = backend()
    assertEquals("accepted", demo.find(id)?.status)
    now = now.plusSeconds(10)
    val completed = requireNotNull(demo.find(id))
    assertEquals("published", completed.status)
    assertEquals(accepted.createdAt, completed.createdAt)
    completed.validate()
    assertEquals(7, demo.cachedRecords(DEMO_ACCOUNT_ID).size)
  }

  @Test fun changedPayloadCannotReuseAnAcceptedIdAndOldEntryCallbacksCannotAccessTheNextDemo() = runBlocking {
    val demo = backend()
    val id = UUID.randomUUID().toString()
    demo.submit(id, "架空の議題")
    try { demo.submit(id, "別の架空の議題"); fail("expected payload conflict") }
    catch (error: DebateRequestException) { assertEquals(DebateFailure.CONFLICT, error.failure) }
    mode.write(ReviewDemoMode(ReviewDemoPhase.RESETTING, entry))
    RecordCacheAccount.clear(context, StorageNamespace.DEMO)
    enterDemo()
    assertFalse(demo.isActive())
    try { demo.submit(UUID.randomUUID().toString(), "古い画面の架空入力"); fail("expected stale entry rejection") }
    catch (error: DebateRequestException) { assertEquals(DebateFailure.AUTH_REQUIRED, error.failure) }
    try { demo.cachedRecords(DEMO_ACCOUNT_ID); fail("expected stale reader rejection") }
    catch (error: RecordReadException) { assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure) }
    val fresh = backend()
    assertTrue(fresh.list().items.isEmpty())
    assertEquals(6, fresh.cachedRecords(DEMO_ACCOUNT_ID).size)
    assertEquals("", fresh.load().draft)
  }

  @Test fun blankQuestionsAndDuplicateOrInvalidImagesAreRejectedWithoutAcceptingARequest() = runBlocking {
    val demo = backend()
    val id = UUID.randomUUID().toString()
    val image = demo.importImage(DEMO_SAMPLE_IMAGE_URI)
    try { demo.submit(id, " "); fail("expected question validation") }
    catch (error: DebateRequestException) { assertEquals(DebateFailure.INVALID_RESPONSE, error.failure) }
    try { demo.submit(id, "架空の議題", listOf(image, image)); fail("expected duplicate image rejection") }
    catch (error: DebateRequestException) { assertEquals(DebateFailure.ATTACHMENT_INVALID, error.failure) }
    val invalid = DraftAttachment(UUID.randomUUID().toString(), "image/png", 1, "invalid", 1, 1)
    try { demo.submit(id, "架空の議題", listOf(invalid)); fail("expected image validation") }
    catch (error: DebateRequestException) { assertEquals(DebateFailure.ATTACHMENT_INVALID, error.failure) }
    assertTrue(demo.list().items.isEmpty())
  }

  @Test fun demoSubmissionAndErasurePreserveEncryptedProductionRowsAndNeverReadTheirAccount() = runBlocking {
    val productionAccount = "p".repeat(43)
    RecordCacheAccount.activate(context, productionAccount, StorageNamespace.PRODUCTION)
    val database = EncryptedRecordsDatabase.open(context, StorageNamespace.PRODUCTION)
    try {
      val production = EncryptedRecordStore(database,
        RecordDataKeyProtector(KeystorePrivateKeyStore(context, StorageNamespace.PRODUCTION)))
      val sentinel = "fictional-production-sentinel".encodeToByteArray()
      production.save(productionAccount, "sentinel", CachedRecordPart.DETAIL, sentinel)
      val demo = backend()
      demo.submit(UUID.randomUUID().toString(), "架空の隔離確認")
      try { demo.cachedRecords(productionAccount); fail("expected production owner rejection") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure) }
      mode.write(ReviewDemoMode(ReviewDemoPhase.EXITING, entry))
      RecordCacheAccount.clear(context, StorageNamespace.DEMO)
      assertArrayEquals(sentinel, production.load(productionAccount, "sentinel", CachedRecordPart.DETAIL))
      sentinel.fill(0)
    } finally { database.close() }
  }
}
