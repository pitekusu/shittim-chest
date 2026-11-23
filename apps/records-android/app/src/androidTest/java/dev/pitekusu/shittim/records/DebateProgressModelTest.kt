package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DebateProgressModelTest {
  private val id = "11111111-2222-4333-8444-abcdefabcdef"
  private val second = "99999999-2222-4333-8444-abcdefabcdef"
  private fun request(requestId: String = id, status: String = "queued", attachmentIds: List<String> = emptyList()) = DebateRequest(
    requestId, "架空の議題", status, createdAt = "2026-10-04T00:00:00Z", updatedAt = "2026-10-04T00:00:00Z",
    recordId = "r".repeat(43).takeIf { status == "published" }, attachmentIds = attachmentIds)
  private suspend fun await(condition: () -> Boolean) = withTimeout(5000) { while (!condition()) delay(10) }
  private suspend fun clear(model: DebateSubmissionModel): Unit = withContext(Dispatchers.Main) {
    ViewModelStore().apply { put("model", model); clear() }
    Unit
  }

  @Test fun restoredRequestLooksUpTheResultAndUnchangedPollingDoesNotRewriteCiphertext() = runBlocking {
    var saved = DebateWorkspace(draft = "架空の議題", requestId = id, frozenQuestion = "架空の議題")
    var writes = 0
    var posts = 0
    val model = DebateSubmissionModel({ true }, { saved }, { saved = it; writes++ },
      { _, _ -> posts++; null }, { request(it, "published") }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    withContext(Dispatchers.Main) { model.refreshStatus(id); model.refreshStatus(id) }
    assertEquals("published", model.status.value.request?.status)
    assertEquals("r".repeat(43), model.status.value.request?.recordId)
    assertEquals(1, writes)
    assertEquals(0, posts)
    assertEquals(id, saved.receipt?.requestId)
    clear(model)
  }

  @Test fun waitingForImageRestorationLetsTheStatusCheckOpenANewDraft() = runBlocking<Unit> {
    val preview = byteArrayOf(1, 2)
    val image = DraftAttachment(second, "image/png", preview.size, attachmentDigest(preview), 1, 2)
    val queued = request(attachmentIds = listOf(image.attachmentId))
    val published = request(status = "published", attachmentIds = queued.attachmentIds)
    var saved = DebateWorkspace(draft = queued.question, requestId = id, frozenQuestion = queued.question,
      receipt = queued, attachments = listOf(image))
    val readingPreview = CompletableDeferred<Unit>()
    val finishPreview = CompletableDeferred<Unit>()
    var lookups = 0
    val model = DebateSubmissionModel({ true }, { saved }, { saved = it }, { _, _ -> null },
      { lookups++; published }, {}, readImage = {
        readingPreview.complete(Unit)
        finishPreview.await()
        preview
      })
    try {
      withContext(Dispatchers.Main) { model.restore() }
      withTimeout(5000) { readingPreview.await() }
      assertSame(queued, model.state.value.workspace?.receipt)
      withContext(Dispatchers.Main) {
        val lookup = async(start = CoroutineStart.UNDISPATCHED) {
          model.awaitRestoration()
          model.refreshStatus(id)
        }
        assertFalse(lookup.isCompleted)
        assertEquals(0, lookups)
        finishPreview.complete(Unit)
        withTimeout(5000) { lookup.await() }
        assertEquals(1, lookups)
        assertSame(published, model.state.value.workspace?.receipt)
        assertTrue(model.newDraft())
      }
      await { !model.state.value.saving }
      assertNull(saved.requestId)
      assertEquals("", saved.draft)
      assertTrue(saved.attachments.isEmpty())
    } finally {
      clear(model)
    }
  }

  @Test fun cancelledForegroundReadDoesNotFinishOrSendAndCanResume() = runBlocking {
    val started = CompletableDeferred<Unit>()
    val response = CompletableDeferred<DebateRequest?>()
    var posts = 0
    val model = DebateSubmissionModel({ true }, { DebateWorkspace() }, {},
      { _, _ -> posts++; null }, { started.complete(Unit); response.await() }, {})
    val read = async(Dispatchers.Main) { model.refreshStatus(id) }
    started.await()
    read.cancelAndJoin()
    assertFalse(model.status.value.loading)
    assertNull(model.status.value.request)
    response.complete(request())
    withContext(Dispatchers.Main) { model.refreshStatus(id) }
    assertEquals("queued", model.status.value.request?.status)
    assertEquals(0, posts)
    clear(model)
  }

  @Test fun paginatedInventoryDeduplicatesAndKeepsLoadedHistoryOnFailure() = runBlocking {
    val cursors = mutableListOf<String?>()
    val model = DebateSubmissionModel({ true }, { DebateWorkspace() }, {}, { _, _ -> null }, { null }, {},
      { cursor ->
        cursors += cursor
        when (cursors.size) {
          1 -> DebateRequestPage(listOf(request()), "opaque_cursor")
          2 -> DebateRequestPage(listOf(request(), request(second)))
          else -> throw DebateRequestException(DebateFailure.UNAVAILABLE)
        }
      })
    withContext(Dispatchers.Main) { model.refreshHistory(); model.refreshHistory(more = true); model.refreshHistory() }
    assertEquals(listOf(null, "opaque_cursor", null), cursors)
    assertEquals(setOf(id, second), model.history.value.items.map { it.requestId }.toSet())
    assertEquals(DebateFailure.UNAVAILABLE, model.history.value.failure)
    assertFalse(model.history.value.loading)
    clear(model)
  }

  @Test fun permissionUpgradeKeepsFrozenPayloadAfterMissingLookupAndNeverRekeys() = runBlocking {
    var saved = DebateWorkspace(draft = "架空の議題")
    val model = DebateSubmissionModel({ true }, { saved }, { saved = it },
      { _, _ -> throw DebateRequestException(DebateFailure.REAUTH_REQUIRED) }, { null }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    withContext(Dispatchers.Main) { model.submit() }
    await { !model.state.value.busy }
    val frozenId = requireNotNull(saved.requestId)
    withContext(Dispatchers.Main) { model.refreshStatus(frozenId) }
    assertEquals(DebateFailure.REAUTH_REQUIRED, model.state.value.failure)
    assertEquals(frozenId, model.state.value.workspace?.requestId)
    assertEquals("架空の議題", model.state.value.workspace?.frozenQuestion)
    assertTrue(model.state.value.confirmedMissing)
    clear(model)
  }

  @Test fun lateReadCannotRepopulateAnAccountAfterItsPermitIsLost() = runBlocking {
    var allowed = true
    val started = CompletableDeferred<Unit>()
    val response = CompletableDeferred<DebateRequest?>()
    val model = DebateSubmissionModel({ allowed }, { DebateWorkspace() }, {}, { _, _ -> null },
      { started.complete(Unit); response.await() }, {})
    val read = async(Dispatchers.Main) { model.refreshStatus(id) }
    started.await()
    withContext(Dispatchers.Main) { allowed = false; model.hide() }
    response.complete(request())
    read.await()
    assertNull(model.status.value.request)
    assertNull(model.state.value.workspace)
    assertTrue(model.history.value.items.isEmpty())
    clear(model)
  }

  @Test fun explicitRetryRechecksAnEarlierMissingRequestBeforePosting() = runBlocking {
    var lookups = 0
    var posts = 0
    val model = DebateSubmissionModel({ true },
      { DebateWorkspace(draft = "架空の議題", requestId = id, frozenQuestion = "架空の議題") }, {},
      { _, _ -> posts++; request() }, { if (++lookups == 1) null else request() }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    withContext(Dispatchers.Main) { model.reconcile() }
    await { model.state.value.confirmedMissing }
    withContext(Dispatchers.Main) { model.retryConfirmedMissing() }
    await { model.state.value.workspace?.receipt != null }
    assertEquals(2, lookups)
    assertEquals(0, posts)
    clear(model)
  }

  @Test fun newDraftCannotBeReplacedByAnOlderPollingWrite() = runBlocking {
    var saved = DebateWorkspace(draft = "架空の議題", requestId = id,
      frozenQuestion = "架空の議題", receipt = request(status = "failed"))
    val writing = CompletableDeferred<Unit>()
    val finishWrite = CompletableDeferred<Unit>()
    val model = DebateSubmissionModel({ true }, { saved }, { value ->
      if (value.receipt != null) { writing.complete(Unit); finishWrite.await() }
      saved = value
    }, { _, _ -> null }, { request(status = "running") }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    val poll = async(Dispatchers.Main) { model.refreshStatus(id) }
    writing.await()
    withContext(Dispatchers.Main) { model.newDraft() }
    finishWrite.complete(Unit)
    poll.await()
    await { !model.state.value.saving }
    assertNull(model.state.value.workspace?.requestId)
    assertNull(saved.requestId)
    assertEquals("", saved.draft)
    clear(model)
  }

  @Test fun terminalHistoryCannotCreateANewDraftOverAnotherActiveRequestOrDraft() = runBlocking<Unit> {
    for (workspace in listOf(
      DebateWorkspace(draft = "架空の議題", requestId = id, frozenQuestion = "架空の議題", receipt = request(status = "running")),
      DebateWorkspace(draft = "架空の未送信下書き"),
    )) {
      var saves = 0
      var navigated = false
      val model = DebateSubmissionModel({ true }, { workspace }, { saves++ },
        { _, _ -> null }, { request(it, "published") }, {})
      withContext(Dispatchers.Main) { model.restore() }
      await { model.state.value.workspace != null }
      withContext(Dispatchers.Main) {
        model.refreshStatus(second)
        if (model.newDraft()) navigated = true
      }
      assertTrue(requireNotNull(model.status.value.request).terminal)
      assertFalse(navigated)
      assertSame(workspace, model.state.value.workspace)
      assertEquals(0, saves)
      clear(model)
    }
  }

  @Test fun newDraftNavigatesOnlyWhenCreatedAndKeepsStorageFailureVisible() = runBlocking<Unit> {
    for (storageFails in listOf(false, true)) {
      var saved = DebateWorkspace(draft = "架空の議題", requestId = id,
        frozenQuestion = "架空の議題", receipt = request(status = "published"))
      var navigated = false
      val model = DebateSubmissionModel({ true }, { saved }, {
        if (storageFails) throw IllegalStateException("synthetic_storage_failure")
        saved = it
      }, { _, _ -> null }, { null }, {})
      withContext(Dispatchers.Main) { model.restore() }
      await { model.state.value.workspace != null }
      withContext(Dispatchers.Main) { if (model.newDraft()) navigated = true }
      await { !model.state.value.saving }
      assertTrue(navigated)
      assertNull(model.state.value.workspace?.requestId)
      assertEquals("", model.state.value.workspace?.draft)
      assertEquals(if (storageFails) DebateFailure.STORAGE else null, model.state.value.failure)
      assertEquals(if (storageFails) id else null, saved.requestId)
      clear(model)
    }
  }

  @Test fun restoredPublishedReceiptOpensOfflineOrAfterUnavailableReadOnlyForThePermittedRequest() = runBlocking<Unit> {
    var allowed = true
    val receipt = request(status = "published")
    val saved = DebateWorkspace(draft = receipt.question, requestId = id,
      frozenQuestion = receipt.question, receipt = receipt)
    val model = DebateSubmissionModel({ allowed }, { saved }, {}, { _, _ -> null },
      { throw DebateRequestException(DebateFailure.UNAVAILABLE) }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    assertNull(model.status.value.request)
    assertEquals("r".repeat(43), model.publishedRecordId(id))
    assertSame(receipt, debateReceipt(id, model.status.value, model.state.value))
    withContext(Dispatchers.Main) { model.refreshStatus(id) }
    assertEquals(DebateFailure.UNAVAILABLE, model.status.value.failure)
    assertEquals("r".repeat(43), model.publishedRecordId(id))
    assertNull(model.publishedRecordId(second))
    withContext(Dispatchers.Main) { allowed = false }
    assertNull(model.publishedRecordId(id))
    clear(model)
  }

  @Test fun latestNonPublishedStatusAndMisboundReceiptsCannotOpenAnArchivedResult() = runBlocking<Unit> {
    val receipt = request(status = "published")
    val saved = DebateWorkspace(draft = receipt.question, requestId = id,
      frozenQuestion = receipt.question, receipt = receipt)
    val model = DebateSubmissionModel({ true }, { saved }, {}, { _, _ -> null }, { request() }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    withContext(Dispatchers.Main) { model.refreshStatus(id) }
    assertNull(model.publishedRecordId(id))
    assertNull(debateReceipt(second, DebateStatusState(second, receipt), DebateSubmissionState(saved)))
    assertNull(debateReceipt(id, DebateStatusState(), DebateSubmissionState(
      DebateWorkspace(requestId = second, frozenQuestion = receipt.question, receipt = receipt))))
    clear(model)
  }
}
