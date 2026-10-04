package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DebateSubmissionModelTest {
  private val id = "11111111-2222-4333-8444-abcdefabcdef"
  private fun receipt(id: String, question: String) = DebateRequest(id, question, "queued",
    createdAt = "2026-10-04T00:00:00Z", updatedAt = "2026-10-04T00:00:00Z")
  private suspend fun await(condition: () -> Boolean) = withTimeout(5000) {
    while (!condition()) delay(10)
  }

  @Test fun storageFailurePreventsAnyPost() = runBlocking<Unit> {
    var calls = 0
    val model = DebateSubmissionModel({ true }, { DebateWorkspace(draft = "架空の議題") },
      { throw IllegalStateException("synthetic_storage_failure") },
      { _, _ -> calls++; null }, { null }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    withContext(Dispatchers.Main) { model.edit("架空の編集済み議題"); model.submit(); model.submit() }
    await { !model.state.value.busy }
    assertEquals(0, calls)
    assertEquals(DebateFailure.STORAGE, model.state.value.failure)
    assertNull(model.state.value.workspace?.requestId)
    var navigated = false
    withContext(Dispatchers.Main) { model.flush { navigated = true } }
    assertFalse(navigated) // A loaded draft still requires a successful durable write.
    withContext(Dispatchers.Main) { ViewModelStore().apply { put("test", model); clear() } }
  }

  @Test fun ambiguousPostSurvivesRestartWithoutAutomaticReplayOrRekey() = runBlocking<Unit> {
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

  @Test fun onlyFreshMissingLookupAllowsExplicitSamePayloadResend() = runBlocking<Unit> {
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

  @Test fun backWaitsForFrozenWriteButNotForPostResponse() = runBlocking<Unit> {
    val saving = CompletableDeferred<Unit>()
    val saved = CompletableDeferred<Unit>()
    val response = CompletableDeferred<DebateRequest?>()
    var navigated = false
    val model = DebateSubmissionModel({ true }, { DebateWorkspace(draft = "架空の議題") },
      { saving.complete(Unit); saved.await() }, { _, _ -> response.await() }, { null }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.workspace != null }
    withContext(Dispatchers.Main) { model.submit(); model.flush { navigated = true } }
    saving.await()
    assertFalse(navigated)
    saved.complete(Unit)
    await { navigated }
    assertTrue(model.state.value.busy)
    assertFalse(response.isCompleted)
    withContext(Dispatchers.Main) { ViewModelStore().apply { put("model", model); clear() } }
  }

  @Test fun failedWorkspaceRestorationDoesNotTrapBackOrWriteAnEmptyReplacement() = runBlocking<Unit> {
    var saves = 0
    var navigated = false
    val model = DebateSubmissionModel({ true }, { throw IllegalStateException("synthetic_storage_failure") },
      { saves++ }, { _, _ -> null }, { null }, {})
    withContext(Dispatchers.Main) { model.restore() }
    await { model.state.value.failure == DebateFailure.STORAGE }
    withContext(Dispatchers.Main) { model.flush { navigated = true } }
    assertTrue(navigated)
    assertEquals(0, saves)
    withContext(Dispatchers.Main) { ViewModelStore().apply { put("model", model); clear() } }
  }

  @Test fun backDuringWorkspaceRestorationDoesNotWaitForOrOverwriteTheRead() = runBlocking<Unit> {
    val loading = CompletableDeferred<Unit>()
    val loaded = CompletableDeferred<DebateWorkspace>()
    var saves = 0
    var navigated = false
    val model = DebateSubmissionModel({ true }, { loading.complete(Unit); loaded.await() },
      { saves++ }, { _, _ -> null }, { null }, {})
    withContext(Dispatchers.Main) { model.restore() }
    loading.await()
    withContext(Dispatchers.Main) { model.flush { navigated = true } }
    assertTrue(navigated)
    assertEquals(0, saves)
    loaded.complete(DebateWorkspace(draft = "架空の保存済み下書き"))
    await { model.state.value.workspace != null }
    assertEquals("架空の保存済み下書き", model.state.value.workspace?.draft)
    assertEquals(0, saves)
    withContext(Dispatchers.Main) { ViewModelStore().apply { put("model", model); clear() } }
  }

  @Test fun submissionAndLookupRouteAuthenticationDenialToTheExistingSessionLock() = runBlocking<Unit> {
    for (lookup in listOf(false, true)) {
      var permitted = true
      var locked = 0
      var saved = if (lookup) DebateWorkspace(draft = "架空の議題", requestId = id, frozenQuestion = "架空の議題")
        else DebateWorkspace(draft = "架空の議題")
      val model = DebateSubmissionModel({ permitted }, { saved }, { saved = it },
        { _, _ -> throw DebateRequestException(DebateFailure.AUTH_REQUIRED) },
        { throw DebateRequestException(DebateFailure.AUTH_REQUIRED) }, {},
        authenticationRequired = { locked++; permitted = false })
      withContext(Dispatchers.Main) { model.restore() }
      await { model.state.value.workspace != null }
      withContext(Dispatchers.Main) { if (lookup) model.reconcile() else model.submit() }
      await { locked == 1 }
      assertFalse(permitted)
      assertEquals("架空の議題", saved.frozenQuestion) // Denial locks reads, without deleting the draft.
      withContext(Dispatchers.Main) { ViewModelStore().apply { put("model", model); clear() } }
    }
  }

  @Test fun channelOrLegacyPermissionFailureKeepsRecordAuthorization() = runBlocking<Unit> {
    for (failure in listOf(DebateFailure.FORBIDDEN, DebateFailure.REAUTH_REQUIRED)) {
      var locked = 0
      var saved = DebateWorkspace(draft = "架空の議題")
      val model = DebateSubmissionModel({ true }, { saved }, { saved = it },
        { _, _ -> throw DebateRequestException(failure) }, { null }, {},
        authenticationRequired = { locked++ })
      withContext(Dispatchers.Main) { model.restore() }
      await { model.state.value.workspace != null }
      withContext(Dispatchers.Main) { model.submit() }
      await { !model.state.value.busy }
      assertEquals(failure, model.state.value.failure)
      assertEquals(0, locked)
      assertEquals("架空の議題", saved.frozenQuestion)
      withContext(Dispatchers.Main) { ViewModelStore().apply { put("model", model); clear() } }
    }
  }
}
