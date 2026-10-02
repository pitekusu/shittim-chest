package dev.pitekusu.shittim.records

import java.security.MessageDigest
import java.util.Base64
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

// Serializes immediate/periodic workers across repository/Activity instances.
private val recordSyncLock = Mutex()

/** Coroutines handle cancellation; encrypted page-sized checkpoints handle process restart. */
internal suspend fun RecordsRepository.synchronize(token: String, accountId: String,
  onSaved: suspend () -> Unit = {}): Unit = recordSyncLock.withLock {
  val stored = syncCheckpoint(accountId)
  var progress = stored?.takeUnless { it.complete }
    ?: RecordSyncCheckpoint(committedRevisions = stored?.committedRevisions.orEmpty())
  val completeIds = cachedCompleteRecordIds(accountId).toMutableSet()
  // C31 checkpoints cannot prove which earlier pages were seen: restart, never guess deletions.
  if (progress.removalCandidates == null || !progress.indexBased) {
    progress = progress.copy(cursor = null, pendingIds = emptyList(), pendingReferences = emptyList(),
      pageLoaded = false, cursorHashes = emptySet(),
      removalCandidates = cachedRecordIds(accountId), indexBased = true)
    saveSyncCheckpoint(accountId, progress)
  }

  suspend fun consume(reference: RecordSyncReference) {
    currentCoroutineContext().ensureActive()
    val id = reference.recordId
    // Legacy revisions are imported once. Existence is a SQL check, not a full
    // body decryption; actual reads still authenticate the encrypted payload.
    val revision = progress.committedRevisions[id] ?: if (id in completeIds) cachedRevision(accountId, id) else null
    if (revision == reference.revision && id in completeIds) {
      if (id !in progress.committedRevisions) {
        progress = progress.copy(committedRevisions = progress.committedRevisions + (id to reference.revision))
      }
    } else {
      try {
        val previousAvatar = cachedListEntry(accountId, id)?.requesterAvatar
        val result = record(token, accountId, id, reference.avatarRevision)
        if (result !is RecordReadResult.Found || result.listEntry == null) {
          throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
        }
        completeIds += id
        val nextAvatarUrl = result.listEntry.requesterAvatar.url
        val replacedAvatar = previousAvatar?.url?.let { previousUrl ->
          nextAvatarUrl == null || previousAvatar.revision != reference.avatarRevision ||
            avatarSource(previousUrl) != avatarSource(nextAvatarUrl)
        } == true
        // Commit only after both body and list are saved. Icons have a separate,
        // durable retry queue, so a slow image cannot delay the latest result.
        progress = progress.copy(committedRevisions = progress.committedRevisions + (id to reference.revision),
          pendingAvatars = if (result.listEntry.requesterAvatar.url != null) progress.pendingAvatars + id
            else progress.pendingAvatars - id,
          needsAvatarPrune = progress.needsAvatarPrune || replacedAvatar,
          pendingIds = progress.pendingIds.filterNot { it == id },
          pendingReferences = progress.pendingReferences.filterNot { it.recordId == id })
        // A checkpoint failure must not hide an already committed body/list.
        // The reader still gates this notification with its current authorization.
        try { saveSyncCheckpoint(accountId, progress) } finally { onSaved() }
        return
      } catch (error: RecordReadException) {
        if (error.failure != RecordReadFailure.NOT_FOUND) throw error
        completeIds -= id
        progress = progress.copy(committedRevisions = progress.committedRevisions - id,
          pendingAvatars = progress.pendingAvatars - id, needsAvatarPrune = true)
        onSaved()
      }
    }
    progress = progress.copy(pendingIds = progress.pendingIds.filterNot { it == id },
      pendingReferences = progress.pendingReferences.filterNot { it.recordId == id })
    // Unchanged references are checkpointed at page boundaries, not once per record.
  }

  // Check today's head before continuing yesterday's interrupted tail. Keep the
  // original cursor/removal candidates: this extra page is not a complete index.
  if (stored != null && !stored.complete && (progress.pageLoaded || progress.cursor != null)) {
    val latest = syncIndex(token, accountId, null)
    progress = progress.copy(removalCandidates = progress.removalCandidates.orEmpty() - latest.items.map { it.recordId }.toSet())
    for (reference in latest.items) consume(reference)
    saveSyncCheckpoint(accountId, progress)
  }

  var restartedCursor = false
  while (true) {
    currentCoroutineContext().ensureActive()
    if (!progress.pageLoaded) {
      val page = try {
        syncIndex(token, accountId, progress.cursor)
      } catch (error: RecordReadException) {
        if (error.failure != RecordReadFailure.CURSOR_INVALID || progress.cursor == null || restartedCursor) throw error
        restartedCursor = true
        progress = progress.copy(cursor = null, pendingIds = emptyList(), pendingReferences = emptyList(),
          pageLoaded = false, cursorHashes = emptySet(), removalCandidates = cachedRecordIds(accountId))
        saveSyncCheckpoint(accountId, progress)
        continue
      }
      val hashes = progress.cursorHashes + listOfNotNull(progress.cursor?.let(::cursorHash))
      if (page.nextCursor?.let(::cursorHash) in hashes) throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      progress = progress.copy(cursor = page.nextCursor, pendingIds = page.items.map { it.recordId },
        pendingReferences = page.items, pageLoaded = true, cursorHashes = hashes,
        removalCandidates = progress.removalCandidates.orEmpty() - page.items.map { it.recordId }.toSet())
      saveSyncCheckpoint(accountId, progress) // Save the page before consuming its changed records.
    }
    val reference = progress.pendingReferences.firstOrNull()
    if (reference != null) {
      consume(reference)
    } else if (progress.cursor == null) {
      // GSI omissions are not proof of deletion: only an authenticated detail 404
      // may remove data. Keep each candidate until its confirmation succeeds.
      for (candidate in progress.removalCandidates.orEmpty()) {
        currentCoroutineContext().ensureActive()
        removeIfDeleted(token, accountId, candidate)
        progress = progress.copy(removalCandidates = progress.removalCandidates.orEmpty() - candidate,
          needsAvatarPrune = true)
        try { saveSyncCheckpoint(accountId, progress) } finally { onSaved() }
      }
      // All result bodies are visible before any new avatar downloads start.
      saveSyncCheckpoint(accountId, progress)
      for (id in progress.pendingAvatars.toList()) {
        currentCoroutineContext().ensureActive()
        cachedListEntry(accountId, id)?.let { saveAvatar(accountId, it) }
        progress = progress.copy(pendingAvatars = progress.pendingAvatars - id)
        try { saveSyncCheckpoint(accountId, progress) } finally { onSaved() }
      }
      if (progress.needsAvatarPrune) pruneAvatars(accountId)
      val retained = cachedCompleteRecordIds(accountId)
      saveSyncCheckpoint(accountId, progress.copy(complete = true, removalCandidates = emptySet(),
        committedRevisions = progress.committedRevisions.filterKeys { it in retained },
        needsAvatarPrune = false))
      return@withLock
    } else {
      // If interrupted here, the persisted page is harmlessly rechecked using its
      // committed manifest. Avoid a second encrypted write for the page transition.
      progress = progress.copy(pageLoaded = false)
    }
  }
}

private fun cursorHash(cursor: String): String = Base64.getUrlEncoder().withoutPadding()
  .encodeToString(MessageDigest.getInstance("SHA-256").digest(cursor.toByteArray(Charsets.US_ASCII)))
