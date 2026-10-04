package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DebateSubmissionModelTest {
  private val id = "11111111-2222-4333-8444-555555555555"
  private fun receipt(id: String, question: String) = DebateRequest(id, question, "queued",
    createdAt = "2026-10-04T00:00:00Z", updatedAt = "2026-10-04T00:00:00Z")
  private suspend fun await(condition: () -> Boolean) = withTimeout(5000) {
    while (!condition()) delay(10)
  }

  @Test fun storageFailurePreventsAnyPost() = runBlocking {
    var calls = 0
    val model = DebateSubmissionModel({ true }, { DebateWorkspace(draft = "架空の議題") },
      { throw IllegalStateException("synthetic_storage_failure") },
      { _, _ -> calls++; null }, { null }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    withContext(Dispatchers.Main) { model.submit(); model.submit() }
    await { !model.state.value.busy }
    assertEquals(0, calls)
    assertEquals(DebateFailure.STORAGE, model.state.value.failure)
    assertNull(model.state.value.workspace?.requestId)
    withContext(Dispatchers.Main) { ViewModelStore().apply { put("test", model); clear() } }
  }

  @Test fun ambiguousPostSurvivesRestartWithoutAutomaticReplayOrRekey() = runBlocking {
    var saved = DebateWorkspace(draft = "架空の議題")
    var calls = 0
    val first = DebateSubmissionModel({ true }, { saved }, { saved = it },
      { requestId, question ->
        assertEquals(requestId, saved.requestId)
        assertEquals(question, saved.frozenQuestion)
        calls++
        throw DebateRequestException(DebateFailure.UNAVAILABLE)
      }, { requestId -> receipt(requestId, "架空の議題") }, {})
    withContext(Dispatchers.Main) { first.restore() }
    await { first.state.value.workspace != null }
    withContext(Dispatchers.Main) { first.submit(); first.submit() }
    await { !first.state.value.busy }
    assertEquals(1, calls)
    assertTrue(validDebateRequestId(requireNotNull(saved.requestId)))
    val sameId = saved.requestId
    withContext(Dispatchers.Main) { ViewModelStore().apply { put("first", first); clear() } }
    val second = DebateSubmissionModel({ true }, { saved }, { saved = it },
      { _, _ -> calls++; null }, { requestId -> receipt(requestId, "架空の議題") }, {})
    withContext(Dispatchers.Main) { second.restore() }
    await { second.state.value.workspace != null }
    withContext(Dispatchers.Main) { second.submit(); second.retryConfirmedMissing(); second.edit("変えられない") }
    assertEquals(1, calls)
    assertEquals(sameId, second.state.value.workspace?.requestId)
    withContext(Dispatchers.Main) { second.reconcile() }
    await { second.state.value.workspace?.receipt != null }
    assertEquals(sameId, saved.receipt?.requestId)
    assertEquals(1, calls)
    withContext(Dispatchers.Main) { ViewModelStore().apply { put("second", second); clear() } }
  }

  @Test fun onlyFreshMissingLookupAllowsExplicitSamePayloadResend() = runBlocking {
    var saved = DebateWorkspace(draft = "架空の議題", requestId = id, frozenQuestion = "架空の議題")
    var calls = 0
    val model = DebateSubmissionModel({ true }, { saved }, { saved = it },
      { requestId, question -> calls++; assertEquals(id, requestId); receipt(requestId, question) }, { null }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    withContext(Dispatchers.Main) { model.retryConfirmedMissing() }
    assertEquals(0, calls)
    withContext(Dispatchers.Main) { model.reconcile() }
    await { model.state.value.confirmedMissing }
    withContext(Dispatchers.Main) { model.retryConfirmedMissing(); model.retryConfirmedMissing() }
    await { saved.receipt != null }
    assertEquals(1, calls)
    assertEquals(id, saved.requestId)
    withContext(Dispatchers.Main) { ViewModelStore().apply { put("model", model); clear() } }
  }
}
