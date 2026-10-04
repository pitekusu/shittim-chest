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

/** The request ID and exact body reach encrypted storage before any network side effect. */
internal class DebateSubmissionModel(
  private val authorized: () -> Boolean,
  private val load: suspend () -> DebateWorkspace,
  private val save: suspend (DebateWorkspace) -> Unit,
  private val send: suspend (String, String) -> DebateRequest?,
  private val find: suspend (String) -> DebateRequest?,
  private val close: () -> Unit,
) : ViewModel() {
  private val mutable = MutableStateFlow(DebateSubmissionState())
  val state = mutable.asStateFlow()
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
    val value = mutable.value.workspace ?: return
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

  fun reconcile() {
    val current = mutable.value
    val value = current.workspace ?: return
    val id = value.requestId ?: return
    if (!authorized() || current.busy) return
    mutable.value = DebateSubmissionState(value, busy = true)
    operation = viewModelScope.launch {
      try {
        val receipt = find(id)
        if (!authorized()) return@launch
        if (receipt == null) mutable.value = DebateSubmissionState(value, confirmedMissing = true)
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
        if (authorized()) mutable.value = DebateSubmissionState(value, failure = error.failure)
      } catch (_: Exception) {
        if (authorized()) mutable.value = DebateSubmissionState(value, failure = DebateFailure.STORAGE)
      } finally {
        if (authorized() && mutable.value.busy) mutable.value = DebateSubmissionState(value)
      }
    }
  }

  fun retryConfirmedMissing() {
    val current = mutable.value
    val value = current.workspace ?: return
    if (authorized() && !current.busy && current.confirmedMissing && value.requestId != null && value.receipt == null) transmit(value)
  }

  fun newDraft() {
    val value = mutable.value.workspace ?: return
    if (!authorized() || mutable.value.busy || value.receipt?.terminal != true) return
    val draft = DebateWorkspace()
    mutable.value = DebateSubmissionState(draft, saving = true)
    viewModelScope.launch { persistEdit(draft) }
  }

  fun hide() {
    autosave?.cancel()
    operation?.cancel()
    committed = null
    pendingSave = null
    mutable.value = DebateSubmissionState()
  }

  override fun onCleared() { hide(); close() }
}
