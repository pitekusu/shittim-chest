package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import java.util.UUID
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

internal class DebateSubmissionState(val workspace: DebateWorkspace? = null,
  val busy: Boolean = false, val saving: Boolean = false, val failure: DebateFailure? = null,
  val confirmedMissing: Boolean = false)

internal class DebateStatusState(val requestId: String? = null, val request: DebateRequest? = null,
  val loading: Boolean = false, val failure: DebateFailure? = null)
internal class DebateHistoryState(val items: List<DebateRequest> = emptyList(),
  val nextCursor: String? = null, val loading: Boolean = false, val failure: DebateFailure? = null,
  val loaded: Boolean = false)

/** The request ID and exact body reach encrypted storage before any network side effect. */
internal class DebateSubmissionModel(
  private val authorized: () -> Boolean,
  private val load: suspend () -> DebateWorkspace,
  private val save: suspend (DebateWorkspace) -> Unit,
  private val send: suspend (String, String) -> DebateRequest?,
  private val find: suspend (String) -> DebateRequest?,
  private val close: () -> Unit,
  private val list: suspend (String?) -> DebateRequestPage = { DebateRequestPage(emptyList()) },
  private val authenticationRequired: () -> Unit = {},
) : ViewModel() {
  private val mutable = MutableStateFlow(DebateSubmissionState())
  val state = mutable.asStateFlow()
  private val mutableStatus = MutableStateFlow(DebateStatusState())
  val status = mutableStatus.asStateFlow()
  private val mutableHistory = MutableStateFlow(DebateHistoryState())
  val history = mutableHistory.asStateFlow()
  private val writes = Mutex()
  private var autosave: Job? = null
  private var operation: Job? = null
  private var committed: DebateWorkspace? = null
  private var pendingSave: DebateWorkspace? = null

  fun restore() {
    if (!authorized() || operation?.isActive == true || mutable.value.workspace != null) return
    operation = viewModelScope.launch {
      try {
        val loaded = load()
        if (authorized()) { committed = loaded; mutable.value = DebateSubmissionState(loaded) }
      } catch (error: CancellationException) { throw error }
      catch (_: Exception) { if (authorized()) mutable.value = DebateSubmissionState(failure = DebateFailure.STORAGE) }
    }
  }

  fun edit(text: String) {
    val current = mutable.value
    val value = current.workspace ?: return
    if (!authorized() || current.busy || value.requestId != null || text.codePointCount(0, text.length) > 1000) return
    val edited = DebateWorkspace(draft = text)
    mutable.value = DebateSubmissionState(edited, saving = true)
    autosave?.cancel()
    autosave = viewModelScope.launch {
      delay(300)
      persistEdit(edited)
    }
  }

  private suspend fun persistEdit(edited: DebateWorkspace): Boolean = writes.withLock {
    if (!authorized() || mutable.value.workspace !== edited) return@withLock false
    try {
      save(edited)
      committed = edited
      if (authorized() && mutable.value.workspace === edited) mutable.value = DebateSubmissionState(edited)
      true
    } catch (error: CancellationException) { throw error }
    catch (_: Exception) {
      if (authorized() && mutable.value.workspace === edited) mutable.value = DebateSubmissionState(edited, failure = DebateFailure.STORAGE)
      false
    }
  }

  fun flush(afterSave: () -> Unit) {
    // No edits are possible before restoration. A failed or pending read has no
    // unsaved workspace to protect, so it must not trap Back on this screen.
    val value = mutable.value.workspace ?: run { afterSave(); return }
    val flushingOperation = mutable.value.busy
    if (!flushingOperation && committed === value) { afterSave(); return }
    autosave?.cancel()
    viewModelScope.launch {
      if (flushingOperation) {
        // Wait for an in-flight encrypted write, not for the POST response. A queued
        // freeze must also be durable before Back or permission-upgrade reauthentication.
        val durable = writes.withLock {
          if (!authorized()) return@withLock false
          try {
            pendingSave?.let { pending ->
              if (committed !== pending) { save(pending); committed = pending }
            }
            true
          } catch (error: CancellationException) { throw error }
          catch (_: Exception) { false }
        }
        if (durable && authorized()) afterSave()
      } else if (persistEdit(value) && authorized()) afterSave()
    }
  }

  fun submit() {
    val current = mutable.value
    val value = current.workspace ?: return
    if (!authorized() || current.busy || !validDebateQuestion(value.draft) || value.requestId != null) return
    transmit(DebateWorkspace(draft = value.draft, requestId = UUID.randomUUID().toString(), frozenQuestion = value.draft))
  }

  private fun transmit(frozen: DebateWorkspace) {
    autosave?.cancel()
    pendingSave = frozen
    mutable.value = DebateSubmissionState(mutable.value.workspace, busy = true)
    operation = viewModelScope.launch {
      try {
        writes.withLock {
          if (committed !== frozen) save(frozen)
          committed = frozen
          pendingSave = null
        }
        if (!authorized()) return@launch
        mutable.value = DebateSubmissionState(frozen, busy = true)
        val receipt = send(requireNotNull(frozen.requestId), requireNotNull(frozen.frozenQuestion)) ?: return@launch
        check(receipt.requestId == frozen.requestId && receipt.question == frozen.frozenQuestion)
        val accepted = DebateWorkspace(draft = frozen.draft, requestId = frozen.requestId,
          frozenQuestion = frozen.frozenQuestion, receipt = receipt)
        writes.withLock { save(accepted) }
        committed = accepted
        if (authorized()) mutable.value = DebateSubmissionState(accepted)
      } catch (error: CancellationException) { throw error }
      catch (error: DebateRequestException) {
        if (error.failure == DebateFailure.AUTH_REQUIRED && authorized()) authenticationRequired()
        if (authorized()) mutable.value = DebateSubmissionState(mutable.value.workspace,
          failure = error.failure)
      } catch (_: Exception) {
        if (authorized()) mutable.value = DebateSubmissionState(mutable.value.workspace, failure = DebateFailure.STORAGE)
      } finally {
        if (authorized() && mutable.value.busy) mutable.value = DebateSubmissionState(mutable.value.workspace,
          failure = mutable.value.failure)
      }
    }
  }

  fun reconcile() = lookup(resendIfMissing = false)

  private fun lookup(resendIfMissing: Boolean) {
    val current = mutable.value
    val value = current.workspace ?: return
    val id = value.requestId ?: return
    if (!authorized() || current.busy) return
    mutable.value = DebateSubmissionState(value, busy = true)
    operation = viewModelScope.launch {
      var handedOff = false
      try {
        val receipt = find(id)
        if (!authorized()) return@launch
        if (receipt == null) {
          mutable.value = DebateSubmissionState(value, confirmedMissing = true)
          if (resendIfMissing) {
            // An explicit retry always repeats GET immediately before the same-ID POST.
            // A previously missing response alone is not permission to replay later.
            handedOff = true
            transmit(value)
          }
        }
        else {
          check(receipt.requestId == id && receipt.question == value.frozenQuestion)
          val accepted = DebateWorkspace(draft = value.draft, requestId = id,
            frozenQuestion = value.frozenQuestion, receipt = receipt)
          writes.withLock { save(accepted) }
          committed = accepted
          if (authorized()) mutable.value = DebateSubmissionState(accepted)
        }
      } catch (error: CancellationException) { throw error }
      catch (error: DebateRequestException) {
        if (error.failure == DebateFailure.AUTH_REQUIRED && authorized()) authenticationRequired()
        if (authorized()) mutable.value = DebateSubmissionState(value, failure = error.failure)
      } catch (_: Exception) {
        if (authorized()) mutable.value = DebateSubmissionState(value, failure = DebateFailure.STORAGE)
      } finally {
        if (!handedOff && authorized() && mutable.value.busy) mutable.value = DebateSubmissionState(value)
      }
    }
  }

  fun retryConfirmedMissing() {
    val current = mutable.value
    val value = current.workspace ?: return
    if (authorized() && !current.busy && current.confirmedMissing && value.requestId != null && value.receipt == null)
      lookup(resendIfMissing = true)
  }

  fun newDraft(): Boolean {
    val value = mutable.value.workspace ?: return false
    if (!authorized() || mutable.value.busy || value.receipt?.terminal != true) return false
    val draft = DebateWorkspace()
    mutable.value = DebateSubmissionState(draft, saving = true)
    viewModelScope.launch { persistEdit(draft) }
    return true
  }

  /** Called from the visible NavEntry's coroutine; stopping the entry cancels its HTTP read. */
  suspend fun refreshStatus(id: String) {
    if (!authorized() || mutable.value.busy || operation?.isActive == true) return
    val previous = mutableStatus.value.request.takeIf { mutableStatus.value.requestId == id }
    mutableStatus.value = DebateStatusState(id, previous, loading = true)
    mutable.value.workspace?.takeIf { it.requestId == id }?.let { workspace ->
      mutable.value = DebateSubmissionState(workspace, failure = mutable.value.failure)
    }
    try {
      val receipt = find(id)
      if (!authorized() || mutableStatus.value.requestId != id) return
      val workspace = mutable.value.workspace
      if (workspace?.requestId == id) {
        if (receipt == null) mutable.value = DebateSubmissionState(workspace,
          failure = mutable.value.failure, confirmedMissing = true)
        else {
          if (receipt.requestId != id || receipt.question != workspace.frozenQuestion)
            throw DebateRequestException(DebateFailure.INVALID_RESPONSE)
          // Unchanged polling must not repeatedly wrap and encrypt the same small snapshot.
          val old = workspace.receipt
          if (old == null || old.updatedAt != receipt.updatedAt || old.status != receipt.status ||
            old.phase != receipt.phase || old.recordId != receipt.recordId || old.errorCode != receipt.errorCode) {
            val accepted = DebateWorkspace(draft = workspace.draft, requestId = id,
              frozenQuestion = workspace.frozenQuestion, receipt = receipt)
            writes.withLock {
              if (!authorized() || mutable.value.workspace !== workspace || mutableStatus.value.requestId != id)
                return@withLock
              save(accepted)
              // A new draft may be selected while the encrypted write is in progress.
              // Its queued save must remain current instead of being replaced by this poll.
              if (authorized() && mutable.value.workspace === workspace && mutableStatus.value.requestId == id) {
                committed = accepted
                mutable.value = DebateSubmissionState(accepted)
              }
            }
          }
        }
      }
      mutableStatus.value = DebateStatusState(id, receipt,
        failure = if (receipt == null) DebateFailure.NOT_FOUND else null)
    } catch (error: CancellationException) { throw error }
    catch (error: DebateRequestException) {
      if (authorized() && mutableStatus.value.requestId == id) mutableStatus.value = DebateStatusState(id, previous, failure = error.failure)
    } catch (_: Exception) {
      if (authorized() && mutableStatus.value.requestId == id) mutableStatus.value = DebateStatusState(id, previous, failure = DebateFailure.STORAGE)
    } finally {
      if (mutableStatus.value.requestId == id && mutableStatus.value.loading) mutableStatus.value = DebateStatusState(id, previous)
    }
  }

  suspend fun refreshHistory(more: Boolean = false) {
    if (!authorized() || mutableHistory.value.loading) return
    val previous = mutableHistory.value
    val cursor = if (more) previous.nextCursor ?: return else null
    mutableHistory.value = DebateHistoryState(previous.items, previous.nextCursor, loading = true, loaded = previous.loaded)
    try {
      val page = list(cursor)
      if (!authorized()) return
      val items = (if (more) previous.items + page.items else page.items + previous.items)
        .distinctBy { it.requestId }.sortedByDescending { java.time.Instant.parse(it.createdAt) }
      mutableHistory.value = DebateHistoryState(items, page.nextCursor, loaded = true)
    } catch (error: CancellationException) { throw error }
    catch (error: DebateRequestException) {
      if (authorized()) mutableHistory.value = DebateHistoryState(previous.items, previous.nextCursor,
        failure = error.failure, loaded = previous.loaded)
    } catch (_: Exception) {
      if (authorized()) mutableHistory.value = DebateHistoryState(previous.items, previous.nextCursor,
        failure = DebateFailure.UNAVAILABLE, loaded = previous.loaded)
    } finally {
      if (mutableHistory.value.loading) mutableHistory.value = previous
    }
  }

  fun hide() {
    autosave?.cancel()
    operation?.cancel()
    committed = null
    pendingSave = null
    mutable.value = DebateSubmissionState()
    mutableStatus.value = DebateStatusState()
    mutableHistory.value = DebateHistoryState()
  }

  override fun onCleared() { hide(); close() }
}
