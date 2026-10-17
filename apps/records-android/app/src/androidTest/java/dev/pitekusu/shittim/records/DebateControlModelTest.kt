package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DebateControlModelTest {
  private val id = "11111111-2222-4333-8444-abcdefabcdef"
  private val operation = "99999999-2222-4333-8444-abcdefabcdef"
  private val attempt = "a".repeat(64)
  private fun receipt(status: String = "failed", token: String = attempt) = DebateRequest(
    id, "架空の議題", status, createdAt = "2026-10-04T00:00:00Z", updatedAt = "2026-10-04T00:00:00Z",
    attemptToken = token, canRetry = status == "failed", canCancel = status == "running")
  private fun workspace(request: DebateRequest = receipt(), pending: DebatePendingControl? = null) =
    DebateWorkspace(draft = request.question, requestId = id, frozenQuestion = request.question,
      receipt = request, pendingControl = pending)
  private suspend fun await(condition: () -> Boolean) = withTimeout(5000) { while (!condition()) delay(10) }
  private suspend fun clear(model: DebateSubmissionModel): Unit = withContext(Dispatchers.Main) {
    ViewModelStore().apply { put("control", model); clear() }
    Unit
  }

  @Test fun historyRetryPersistsItsAttemptWithoutReplacingAnUnsentDraftAndGuardsDoubleTap() = runBlocking {
    var saved = DebateWorkspace(draft = "架空の別の下書き")
    var posts = 0
    val sending = CompletableDeferred<Unit>()
    val response = CompletableDeferred<DebateControlResponse>()
    val model = DebateSubmissionModel({ true }, { saved }, { saved = it }, { _, _ -> null }, { receipt() }, {},
      control = { requestId, action, operationId, token ->
        posts++
        assertEquals(id, requestId)
        assertEquals("retry", action)
        assertEquals(attempt, token)
        assertEquals(operationId, saved.pendingControl?.operationId)
        assertEquals("架空の別の下書き", saved.draft)
        assertNull(saved.requestId)
        sending.complete(Unit)
        response.await()
      }, findControl = { _, operationId ->
        DebateControlResponse(operationId, "retry", "completed", request = receipt("running", "b".repeat(64)))
      })
    try {
      withContext(Dispatchers.Main) { model.restore() }
      await { model.state.value.workspace != null }
      withContext(Dispatchers.Main) { model.refreshStatus(id); model.control(id, "retry"); model.control(id, "retry") }
      sending.await()
      assertEquals(1, posts)
      val pending = requireNotNull(saved.pendingControl)
      response.complete(DebateControlResponse(pending.operationId, "retry", "accepted", request = receipt()))
      await { model.status.value.controlPending && !model.status.value.controlBusy }
      withContext(Dispatchers.Main) { model.control(id, "retry"); model.refreshStatus(id) }
      assertEquals(1, posts)
      assertEquals("running", model.status.value.request?.status)
      assertFalse(model.status.value.controlPending)
      assertNull(saved.pendingControl)
      assertEquals("架空の別の下書き", saved.draft)
      assertNull(saved.requestId)
    } finally { clear(model) }
  }

  @Test fun unknownPostRestoresTheSameOperationAndOnlyReadsUntilItsOutcomeIsKnown() = runBlocking {
    var saved = workspace()
    var posts = 0
    val first = DebateSubmissionModel({ true }, { saved }, { saved = it }, { _, _ -> null }, { receipt() }, {},
      control = { _, _, _, _ -> posts++; throw DebateRequestException(DebateFailure.UNAVAILABLE) },
      findControl = { _, _ -> null })
    withContext(Dispatchers.Main) { first.restore() }
    await { first.state.value.workspace != null }
    withContext(Dispatchers.Main) { first.control(id, "retry") }
    await { first.status.value.controlFailure == DebateFailure.UNAVAILABLE }
    assertEquals(1, posts)
    val pending = requireNotNull(saved.pendingControl)
    clear(first)
    var reads = 0
    val second = DebateSubmissionModel({ true }, { saved }, { saved = it }, { _, _ -> null },
      { fail("Pending control must use its operation lookup"); null }, {},
      control = { _, _, _, _ -> posts++; error("automatic replay") },
      findControl = { requestId, operationId ->
        reads++
        assertEquals(id, requestId)
        assertEquals(pending.operationId, operationId)
        DebateControlResponse(operationId, "retry", "completed", request = receipt("running", "b".repeat(64)))
      })
    try {
      withContext(Dispatchers.Main) { second.restore() }
      await { second.state.value.workspace != null }
      assertTrue(second.status.value.controlPending)
      assertEquals(0, reads)
      withContext(Dispatchers.Main) { second.control(id, "retry"); second.refreshStatus(id) }
      assertEquals(1, posts)
      assertEquals(1, reads)
      assertNull(saved.pendingControl)
      assertEquals("running", saved.receipt?.status)
    } finally { clear(second) }
  }

  @Test fun aNewAttemptOrLostCapabilityAtPreflightCannotReceiveTheOldConfirmation() = runBlocking {
    for (fresh in listOf(receipt(token = "b".repeat(64)), receipt("running"))) {
      var saved = workspace()
      var posts = 0
      val model = DebateSubmissionModel({ true }, { saved }, { saved = it }, { _, _ -> null }, { fresh }, {},
        control = { _, _, _, _ -> posts++; error("stale confirmation") }, findControl = { _, _ -> null })
      try {
        withContext(Dispatchers.Main) { model.restore() }
        await { model.state.value.workspace != null }
        withContext(Dispatchers.Main) { model.control(id, "retry") }
        await { model.status.value.controlFailure == DebateFailure.CONFLICT }
        assertEquals(0, posts)
        assertNull(saved.pendingControl)
        assertSame(fresh, model.status.value.request)
      } finally { clear(model) }
    }
  }

  @Test fun missingOperationCanOnlyBeExplicitlyReplayedWithTheSameIdAfterAnotherLookup() = runBlocking {
    for (delayedOriginalFound in listOf(false, true)) {
      val pending = DebatePendingControl(id, operation, "retry", attempt)
      var saved = workspace(pending = pending)
      var reads = 0
      var posts = 0
      val model = DebateSubmissionModel({ true }, { saved }, { saved = it }, { _, _ -> null }, { receipt() }, {},
        control = { requestId, action, operationId, token ->
          posts++
          assertEquals(id, requestId)
          assertEquals("retry", action)
          assertEquals(operation, operationId)
          assertEquals(attempt, token)
          DebateControlResponse(operation, action, "completed", request = receipt("running"))
        }, findControl = { _, _ ->
          reads++
          if (reads > 1 && delayedOriginalFound)
            DebateControlResponse(operation, "retry", "completed", request = receipt("running"))
          else null
        })
      try {
        withContext(Dispatchers.Main) { model.restore() }
        await { model.state.value.workspace != null }
        withContext(Dispatchers.Main) { model.resendControl(id) }
        assertEquals(0, reads)
        assertEquals(0, posts)
        withContext(Dispatchers.Main) { model.refreshStatus(id) }
        assertTrue(model.status.value.controlCanResend)
        assertEquals(0, posts)
        withContext(Dispatchers.Main) { model.resendControl(id); model.resendControl(id) }
        await { saved.pendingControl == null }
        assertEquals(2, reads)
        assertEquals(if (delayedOriginalFound) 0 else 1, posts)
        assertFalse(model.status.value.controlPending)
      } finally { clear(model) }
    }
  }

  @Test fun failedPersistencePreventsControlAndDefinitiveDenialLocksWithoutDeletingPendingData() = runBlocking {
    for (storageFails in listOf(true, false)) {
      var allowed = true
      var locks = 0
      var posts = 0
      var saved = workspace()
      val model = DebateSubmissionModel({ allowed }, { saved }, {
        if (storageFails) error("synthetic_storage_failure")
        saved = it
      }, { _, _ -> null }, { receipt() }, {}, authenticationRequired = { locks++; allowed = false },
        control = { _, _, _, _ -> posts++; throw DebateRequestException(DebateFailure.AUTH_REQUIRED) },
        findControl = { _, _ -> null })
      try {
        withContext(Dispatchers.Main) { model.restore() }
        await { model.state.value.workspace != null }
        withContext(Dispatchers.Main) { model.control(id, "retry") }
        await { if (storageFails) model.status.value.controlFailure == DebateFailure.STORAGE else locks == 1 }
        assertEquals(if (storageFails) 0 else 1, posts)
        assertEquals(if (storageFails) 0 else 1, locks)
        assertEquals(!storageFails, saved.pendingControl != null)
        assertEquals("架空の議題", saved.frozenQuestion)
      } finally { clear(model) }
    }
  }
}
