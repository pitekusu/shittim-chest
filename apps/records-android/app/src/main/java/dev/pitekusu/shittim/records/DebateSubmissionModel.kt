package dev.pitekusu.shittim.records

import android.net.Uri
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
  val loading: Boolean = false, val failure: DebateFailure? = null,
  val controlBusy: Boolean = false, val controlPending: Boolean = false,
  val controlFailure: DebateFailure? = null, val controlCanResend: Boolean = false,
  val controlAction: String? = null)
internal class DebateHistoryState(val items: List<DebateRequest> = emptyList(),
  val nextCursor: String? = null, val loading: Boolean = false, val failure: DebateFailure? = null,
  val loaded: Boolean = false)

// The screen and its click guard must agree when a status read is unavailable.
internal fun debateReceipt(id: String, status: DebateStatusState, submission: DebateSubmissionState): DebateRequest? =
  status.request?.takeIf { status.requestId == id && it.requestId == id }
    ?: submission.workspace?.takeIf { it.requestId == id }?.receipt?.takeIf { it.requestId == id }

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
  private val importImage: (suspend (Uri) -> DraftAttachment)? = null,
  private val readImage: (suspend (DraftAttachment) -> ByteArray)? = null,
  private val sendWithImages: (suspend (String, String, List<DraftAttachment>) -> DebateRequest?)? = null,
  private val control: (suspend (String, String, String, String) -> DebateControlResponse)? = null,
  private val findControl: (suspend (String, String) -> DebateControlResponse?)? = null,
) : ViewModel() {
  private val mutable = MutableStateFlow(DebateSubmissionState())
  val state = mutable.asStateFlow()
  private val mutableImages = MutableStateFlow<List<RecordImagePresentation>>(emptyList())
  val images = mutableImages.asStateFlow()
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
    mutable.value = DebateSubmissionState()
    operation = viewModelScope.launch {
      try {
        val loaded = load()
        if (authorized()) {
          committed = loaded
          mutable.value = DebateSubmissionState(loaded)
          loaded.pendingControl?.let {
            mutableStatus.value = DebateStatusState(it.requestId,
              loaded.receipt?.takeIf { receipt -> receipt.requestId == it.requestId },
              controlPending = true, controlAction = it.action)
          }
          loadPreviews(loaded)
        }
      } catch (error: CancellationException) { throw error }
      catch (_: Exception) { if (authorized()) mutable.value = DebateSubmissionState(failure = DebateFailure.STORAGE) }
    }
  }

  fun edit(text: String) {
    val current = mutable.value
    val value = current.workspace ?: return
    if (!authorized() || current.busy || value.requestId != null || text.codePointCount(0, text.length) > 1000) return
    val edited = DebateWorkspace(draft = text, attachments = value.attachments, pendingControl = value.pendingControl)
    mutable.value = DebateSubmissionState(edited, saving = true)
    autosave?.cancel()
    autosave = viewModelScope.launch {
      delay(300)
      persistEdit(edited)
    }
  }

  fun addImages(uris: List<Uri>) {
    val current = mutable.value
    val workspace = current.workspace ?: return
    val importer = importImage ?: return
    if (!authorized() || current.busy || workspace.requestId != null || uris.isEmpty()) return
    if (workspace.attachments.size + uris.size > MAX_ATTACHMENT_COUNT) {
      mutable.value = DebateSubmissionState(workspace, failure = DebateFailure.ATTACHMENT_INVALID)
      return
    }
    autosave?.cancel()
    mutable.value = DebateSubmissionState(workspace, busy = true)
    operation = viewModelScope.launch {
      try {
        val next = writes.withLock {
          check(authorized())
          val added = uris.map { importer(it) }
          check(authorized())
          DebateWorkspace(draft = workspace.draft, attachments = workspace.attachments + added,
            pendingControl = workspace.pendingControl).also {
            save(it); committed = it
          }
        }
        if (authorized()) { mutable.value = DebateSubmissionState(next); loadPreviews(next) }
      } catch (error: CancellationException) { throw error }
      catch (error: RecordImageException) {
        if (authorized()) mutable.value = DebateSubmissionState(workspace,
          failure = if (error.failure == RecordImageFailure.STORAGE) DebateFailure.STORAGE else DebateFailure.ATTACHMENT_INVALID)
      } catch (_: Exception) {
        if (authorized()) mutable.value = DebateSubmissionState(workspace, failure = DebateFailure.STORAGE)
      } finally {
        if (authorized() && mutable.value.busy) mutable.value = DebateSubmissionState(mutable.value.workspace)
      }
    }
  }

  fun removeImage(attachmentId: String) {
    val current = mutable.value
    val workspace = current.workspace ?: return
    if (!authorized() || current.busy || workspace.requestId != null) return
    val edited = DebateWorkspace(draft = workspace.draft,
      attachments = workspace.attachments.filterNot { it.attachmentId == attachmentId },
      pendingControl = workspace.pendingControl)
    autosave?.cancel()
    mutable.value = DebateSubmissionState(edited, saving = true)
    autosave = viewModelScope.launch { if (persistEdit(edited)) loadPreviews(edited) }
  }

  private suspend fun loadPreviews(workspace: DebateWorkspace) {
    val reader = readImage ?: return
    val loaded = workspace.attachments.map { image ->
      try { RecordImagePresentation(image.descriptor(), reader(image)) }
      catch (error: CancellationException) { throw error }
      catch (_: Exception) { RecordImagePresentation(image.descriptor(), failure = RecordImageFailure.STORAGE) }
    }
    val current = mutable.value.workspace
    // Editing the question creates a new workspace while keeping the selected images.
    // Only a changed ordered image selection makes this asynchronous result obsolete.
    val sameImages = current != null && current.attachments.size == workspace.attachments.size &&
      current.attachments.zip(workspace.attachments).all { (selected, original) ->
        selected.descriptor().sameImage(original.descriptor())
      }
    if (authorized() && sameImages) {
      mutableImages.value.forEach { it.bytes?.fill(0) }
      mutableImages.value = loaded
    } else loaded.forEach { it.bytes?.fill(0) }
  }

  /** Expired uploads can be discarded only after a fresh read proves no debate was accepted. */
  fun discardMissing() {
    val current = mutable.value
    val workspace = current.workspace ?: return
    val id = workspace.requestId ?: return
    if (!authorized() || current.busy || !current.confirmedMissing || workspace.receipt != null) return
    mutable.value = DebateSubmissionState(workspace, busy = true)
    operation = viewModelScope.launch {
      try {
        if (find(id) != null) { if (authorized()) mutable.value = DebateSubmissionState(workspace); return@launch }
        val draft = DebateWorkspace(draft = workspace.draft, pendingControl = workspace.pendingControl)
        writes.withLock { check(authorized()); save(draft); committed = draft }
        if (authorized()) { mutable.value = DebateSubmissionState(draft); loadPreviews(draft) }
      } catch (error: CancellationException) { throw error }
      catch (_: Exception) { if (authorized()) mutable.value = DebateSubmissionState(workspace, failure = DebateFailure.UNAVAILABLE) }
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
    transmit(DebateWorkspace(draft = value.draft, requestId = UUID.randomUUID().toString(),
      frozenQuestion = value.draft, attachments = value.attachments, pendingControl = value.pendingControl))
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
        val receipt = (if (sendWithImages != null) sendWithImages.invoke(requireNotNull(frozen.requestId), requireNotNull(frozen.frozenQuestion), frozen.attachments)
          else send(requireNotNull(frozen.requestId), requireNotNull(frozen.frozenQuestion))) ?: return@launch
        check(receipt.requestId == frozen.requestId && receipt.question == frozen.frozenQuestion && receipt.attachmentIds == frozen.attachments.map { it.attachmentId })
        val accepted = DebateWorkspace(draft = frozen.draft, requestId = frozen.requestId,
          frozenQuestion = frozen.frozenQuestion, receipt = receipt, attachments = frozen.attachments,
          pendingControl = frozen.pendingControl)
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
          check(receipt.requestId == id && receipt.question == value.frozenQuestion && receipt.attachmentIds == value.attachments.map { it.attachmentId })
          val accepted = DebateWorkspace(draft = value.draft, requestId = id,
            frozenQuestion = value.frozenQuestion, receipt = receipt, attachments = value.attachments,
            pendingControl = value.pendingControl)
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
    val draft = DebateWorkspace(pendingControl = value.pendingControl)
    mutable.value = DebateSubmissionState(draft, saving = true)
    viewModelScope.launch { if (persistEdit(draft)) loadPreviews(draft) }
    return true
  }

  fun publishedRecordId(id: String): String? {
    if (!authorized()) return null
    return debateReceipt(id, mutableStatus.value, mutable.value)?.takeIf { it.status == "published" }?.recordId
  }

  /** The confirmation targets the attempt the user saw, never an attempt found later. */
  fun control(id: String, action: String) {
    if (!authorized() || !validDebateControlAction(action) || control == null || findControl == null ||
      mutable.value.busy || operation?.isActive == true || mutableStatus.value.loading ||
      mutable.value.workspace?.pendingControl != null) return
    val workspace = mutable.value.workspace ?: return
    val seen = debateReceipt(id, mutableStatus.value, mutable.value) ?: return
    val attempt = seen.attemptToken ?: return
    if (!allowedControl(seen, action)) return
    autosave?.cancel()
    pendingSave = workspace
    mutable.value = DebateSubmissionState(workspace, busy = true)
    mutableStatus.value = DebateStatusState(id, seen, controlBusy = true, controlAction = action)
    operation = viewModelScope.launch {
      try {
        persistBusyWorkspace()
        val fresh = find(id) ?: throw DebateRequestException(DebateFailure.NOT_FOUND)
        if (!authorized()) return@launch
        try { fresh.validate(); check(fresh.requestId == id) }
        catch (_: Exception) { throw DebateRequestException(DebateFailure.INVALID_RESPONSE) }
        if (fresh.attemptToken != attempt || !allowedControl(fresh, action)) {
          saveControlWorkspace(null, fresh)
          mutableStatus.value = DebateStatusState(id, fresh, controlFailure = DebateFailure.CONFLICT)
          return@launch
        }
        val pending = DebatePendingControl(id, UUID.randomUUID().toString(), action, attempt)
        // Persistence precedes the POST even when this operation belongs to another history item.
        saveControlWorkspace(pending, fresh)
        if (authorized()) sendControl(pending)
      } catch (error: CancellationException) { throw error }
      catch (error: DebateRequestException) { controlFailed(id, error.failure) }
      catch (_: Exception) { controlFailed(id, DebateFailure.STORAGE) }
      finally { finishControlBusy(id) }
    }
  }

  /** Only a fresh operation-404 permits an explicit replay of the same durable operation. */
  fun resendControl(id: String) {
    val state = mutableStatus.value
    val pending = mutable.value.workspace?.pendingControl?.takeIf { it.requestId == id } ?: return
    if (!authorized() || control == null || findControl == null || mutable.value.busy ||
      operation?.isActive == true || state.requestId != id || !state.controlCanResend) return
    val workspace = mutable.value.workspace ?: return
    autosave?.cancel()
    pendingSave = workspace
    mutable.value = DebateSubmissionState(workspace, busy = true)
    mutableStatus.value = DebateStatusState(id, state.request, controlBusy = true,
      controlPending = true, controlAction = pending.action)
    operation = viewModelScope.launch {
      try {
        persistBusyWorkspace()
        // Reconcile again at the explicit replay, since a delayed original POST may now exist.
        val found = findControl.invoke(id, pending.operationId)
        if (!authorized()) return@launch
        if (found == null) sendControl(pending) else acceptControl(pending, found)
      } catch (error: CancellationException) { throw error }
      catch (error: DebateRequestException) { controlFailed(id, error.failure) }
      catch (_: Exception) { controlFailed(id, DebateFailure.STORAGE) }
      finally { finishControlBusy(id) }
    }
  }

  private fun allowedControl(request: DebateRequest, action: String): Boolean =
    if (action == "cancel") request.canCancel else request.canRetry

  private suspend fun sendControl(pending: DebatePendingControl) {
    val response = try {
      requireNotNull(control).invoke(pending.requestId, pending.action, pending.operationId, pending.attemptToken)
    } catch (error: DebateRequestException) {
      if (error.failure == DebateFailure.CONFLICT && authorized()) {
        // A definitive rejection did not create this operation. Refresh the attempt before
        // clearing it so the next confirmation cannot target a stale receipt.
        val fresh = find(pending.requestId)
        if (fresh != null && authorized()) {
          saveControlWorkspace(null, fresh)
          mutableStatus.value = DebateStatusState(pending.requestId, fresh,
            controlFailure = DebateFailure.CONFLICT, controlAction = pending.action)
        }
      }
      throw error
    }
    if (authorized()) acceptControl(pending, response)
  }

  private suspend fun persistBusyWorkspace() = writes.withLock {
    if (!authorized()) return@withLock
    pendingSave?.let {
      if (committed !== it) save(it)
      committed = it
      pendingSave = null
    }
  }

  private suspend fun acceptControl(pending: DebatePendingControl, response: DebateControlResponse) {
    try {
      response.validate()
      check(response.operationId == pending.operationId && response.action == pending.action &&
        response.request.requestId == pending.requestId)
    } catch (_: Exception) { throw DebateRequestException(DebateFailure.INVALID_RESPONSE) }
    val remaining = pending.takeIf { response.status == "accepted" }
    saveControlWorkspace(remaining, response.request)
    if (authorized()) mutableStatus.value = DebateStatusState(pending.requestId, response.request,
      controlPending = remaining != null, controlAction = pending.action,
      controlFailure = if (response.status == "failed") DebateFailure.CONFLICT else null)
  }

  private suspend fun saveControlWorkspace(pending: DebatePendingControl?, request: DebateRequest) = writes.withLock {
    if (!authorized()) return@withLock
    val current = mutable.value.workspace ?: throw DebateRequestException(DebateFailure.STORAGE)
    val ownReceipt = request.takeIf { it.requestId == current.requestId }
    if (ownReceipt != null && (ownReceipt.question != current.frozenQuestion ||
        ownReceipt.attachmentIds != current.attachments.map { it.attachmentId }))
      throw DebateRequestException(DebateFailure.INVALID_RESPONSE)
    if (current.pendingControl?.operationId == pending?.operationId &&
      (ownReceipt == null || sameReceipt(current.receipt, ownReceipt))) return@withLock
    val next = DebateWorkspace(draft = current.draft, requestId = current.requestId,
      frozenQuestion = current.frozenQuestion, receipt = ownReceipt ?: current.receipt,
      attachments = current.attachments, pendingControl = pending)
    save(next)
    if (authorized() && mutable.value.workspace === current) {
      committed = next
      mutable.value = DebateSubmissionState(next, busy = mutable.value.busy, failure = mutable.value.failure)
    }
  }

  private fun controlFailed(id: String, failure: DebateFailure) {
    if (failure == DebateFailure.AUTH_REQUIRED && authorized()) authenticationRequired()
    if (!authorized()) return
    val previous = mutableStatus.value
    val pending = mutable.value.workspace?.pendingControl?.takeIf { it.requestId == id }
    mutableStatus.value = DebateStatusState(id, previous.request?.takeIf { previous.requestId == id },
      controlPending = pending != null, controlFailure = failure, controlAction = pending?.action)
  }

  private fun finishControlBusy(id: String) {
    if (!authorized()) return
    if (mutable.value.busy) mutable.value = DebateSubmissionState(mutable.value.workspace,
      failure = mutable.value.failure)
    val current = mutableStatus.value
    if (current.requestId == id && current.controlBusy) mutableStatus.value = DebateStatusState(
      id, current.request, controlPending = mutable.value.workspace?.pendingControl?.requestId == id,
      controlFailure = current.controlFailure, controlCanResend = current.controlCanResend,
      controlAction = current.controlAction)
  }

  private fun sameReceipt(old: DebateRequest?, receipt: DebateRequest): Boolean = old != null &&
    old.updatedAt == receipt.updatedAt && old.status == receipt.status && old.phase == receipt.phase &&
    old.recordId == receipt.recordId && old.errorCode == receipt.errorCode &&
    old.attemptToken == receipt.attemptToken && old.canCancel == receipt.canCancel && old.canRetry == receipt.canRetry

  private suspend fun refreshControlStatus(id: String, pending: DebatePendingControl) {
    val previous = mutableStatus.value.request.takeIf { mutableStatus.value.requestId == id }
    autosave?.cancel()
    pendingSave = mutable.value.workspace
    mutable.value = DebateSubmissionState(mutable.value.workspace, busy = true, failure = mutable.value.failure)
    mutableStatus.value = DebateStatusState(id, previous, loading = true,
      controlPending = true, controlAction = pending.action)
    try {
      persistBusyWorkspace()
      val found = requireNotNull(findControl).invoke(id, pending.operationId)
      if (!authorized()) return
      if (found == null) mutableStatus.value = DebateStatusState(id, previous,
        controlPending = true, controlFailure = DebateFailure.NOT_FOUND,
        controlCanResend = true, controlAction = pending.action)
      else acceptControl(pending, found)
    } catch (error: CancellationException) { throw error }
    catch (error: DebateRequestException) { controlFailed(id, error.failure) }
    catch (_: Exception) { controlFailed(id, DebateFailure.STORAGE) }
    finally {
      finishControlBusy(id)
      if (mutableStatus.value.requestId == id && mutableStatus.value.loading)
        mutableStatus.value = DebateStatusState(id, previous, controlPending = true, controlAction = pending.action)
    }
  }

  /** Called from the visible NavEntry's coroutine; stopping the entry cancels its HTTP read. */
  suspend fun refreshStatus(id: String) {
    if (!authorized() || mutable.value.busy || operation?.isActive == true) return
    mutable.value.workspace?.pendingControl?.takeIf { it.requestId == id }?.let {
      refreshControlStatus(id, it)
      return
    }
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
          if (receipt.requestId != id || receipt.question != workspace.frozenQuestion || receipt.attachmentIds != workspace.attachments.map { it.attachmentId })
            throw DebateRequestException(DebateFailure.INVALID_RESPONSE)
          // Unchanged polling must not repeatedly wrap and encrypt the same small snapshot.
          val old = workspace.receipt
          if (!sameReceipt(old, receipt)) {
            val accepted = DebateWorkspace(draft = workspace.draft, requestId = id,
              frozenQuestion = workspace.frozenQuestion, receipt = receipt, attachments = workspace.attachments,
              pendingControl = workspace.pendingControl)
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
    mutableImages.value.forEach { it.bytes?.fill(0) }
    mutableImages.value = emptyList()
    mutable.value = DebateSubmissionState()
    mutableStatus.value = DebateStatusState()
    mutableHistory.value = DebateHistoryState()
  }

  override fun onCleared() { hide(); close() }
}
