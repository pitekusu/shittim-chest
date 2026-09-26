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
  var progress = syncCheckpoint(accountId)?.takeUnless { it.complete } ?: RecordSyncCheckpoint()
  // C31 checkpoints cannot prove which earlier pages were seen: restart, never guess deletions.
  if (progress.removalCandidates == null || !progress.indexBased) {
    progress = RecordSyncCheckpoint(removalCandidates = cachedRecordIds(accountId), indexBased = true)
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
        // Signed cursors expire. Keep saved records, but restart enumeration once per attempt.
        restartedCursor = true
        progress = RecordSyncCheckpoint(removalCandidates = cachedRecordIds(accountId), indexBased = true)
        saveSyncCheckpoint(accountId, progress)
        continue
      }
      val hashes = progress.cursorHashes + listOfNotNull(progress.cursor?.let(::cursorHash))
      if (page.nextCursor?.let(::cursorHash) in hashes) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      progress = progress.copy(cursor = page.nextCursor, pendingIds = page.items.map { it.recordId },
        pendingReferences = page.items,
        pageLoaded = true, cursorHashes = hashes,
        removalCandidates = progress.removalCandidates.orEmpty() - page.items.map { it.recordId }.toSet())
      saveSyncCheckpoint(accountId, progress) // Save work before advancing beyond this page.
    }
    val recordId = progress.pendingIds.firstOrNull()
    if (recordId != null) {
      // Authenticated cache reads also deduplicate records repeated across pages or restarts.
      val reference = progress.pendingReferences.first()
      if (cachedRevision(accountId, recordId) != reference.revision || cachedRecord(accountId, recordId) == null) {
        try {
          val result = record(token, accountId, recordId, reference.avatarRevision)
          if (result !is RecordReadResult.Found || result.listEntry == null) {
            throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
          }
          val entry = cachedListEntry(accountId, recordId) ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
          saveAvatar(accountId, entry)
          // Commit the revision last: interrupted icon/body writes must remain eligible.
          saveRevision(accountId, reference)
          onSaved()
        } catch (error: RecordReadException) {
          // The repository removes list/detail only after a confirmed authenticated 404.
          if (error.failure != RecordReadFailure.NOT_FOUND) throw error
        }
      }
      progress = progress.copy(pendingIds = progress.pendingIds.drop(1), pendingReferences = progress.pendingReferences.drop(1))
      saveSyncCheckpoint(accountId, progress) // Interrupted writes can replay, never skip unsaved data.
    } else if (progress.cursor == null) {
      // A complete GSI enumeration can still omit a live record. Only an
      // authenticated detail 404 may remove local data; checkpoint each check.
      for (candidate in progress.removalCandidates.orEmpty()) {
        currentCoroutineContext().ensureActive()
        removeIfDeleted(token, accountId, candidate)
        progress = progress.copy(removalCandidates = progress.removalCandidates.orEmpty() - candidate)
        saveSyncCheckpoint(accountId, progress)
      }
      pruneAvatars(accountId)
      saveSyncCheckpoint(accountId, progress.copy(complete = true, removalCandidates = emptySet()))
      return@withLock
    } else {
      progress = progress.copy(pageLoaded = false)
      saveSyncCheckpoint(accountId, progress)
    }
  }
}

private fun cursorHash(cursor: String): String = Base64.getUrlEncoder().withoutPadding()
  .encodeToString(MessageDigest.getInstance("SHA-256").digest(cursor.toByteArray(Charsets.US_ASCII)))
