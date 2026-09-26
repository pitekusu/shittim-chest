package dev.pitekusu.shittim.records

import java.security.MessageDigest
import java.util.Base64
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

// Serializes foreground sync across repository/Activity instances, not normal paging reads.
private val recordSyncLock = Mutex()

/** Coroutines handle cancellation; encrypted page-sized checkpoints handle process restart. */
internal suspend fun RecordsRepository.synchronize(token: String, accountId: String): Unit = recordSyncLock.withLock {
  var progress = syncCheckpoint(accountId)?.takeUnless { it.complete } ?: RecordSyncCheckpoint()
  // C31 checkpoints cannot prove which earlier pages were seen: restart, never guess deletions.
  if (progress.removalCandidates == null) {
    progress = RecordSyncCheckpoint(removalCandidates = cachedRecordIds(accountId))
    saveSyncCheckpoint(accountId, progress)
  }
  var restartedCursor = false
  while (true) {
    currentCoroutineContext().ensureActive()
    if (!progress.pageLoaded) {
      val page = try {
        recentRecords(token, accountId, progress.cursor)
      } catch (error: RecordReadException) {
        if (error.failure != RecordReadFailure.CURSOR_INVALID || progress.cursor == null || restartedCursor) throw error
        // Signed cursors expire. Keep saved records, but restart enumeration once per attempt.
        restartedCursor = true
        progress = RecordSyncCheckpoint(removalCandidates = cachedRecordIds(accountId))
        saveSyncCheckpoint(accountId, progress)
        continue
      }
      val hashes = progress.cursorHashes + listOfNotNull(progress.cursor?.let(::cursorHash))
      if (page.nextCursor?.let(::cursorHash) in hashes) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      progress = progress.copy(cursor = page.nextCursor, pendingIds = page.items.map { it.recordId },
        pageLoaded = true, cursorHashes = hashes,
        removalCandidates = progress.removalCandidates.orEmpty() - page.items.map { it.recordId }.toSet())
      saveSyncCheckpoint(accountId, progress) // Save work before advancing beyond this page.
    }
    val recordId = progress.pendingIds.firstOrNull()
    if (recordId != null) {
      // Authenticated cache reads also deduplicate records repeated across pages or restarts.
      if (cachedRecord(accountId, recordId) == null) {
        try {
          if (record(token, accountId, recordId) !is RecordReadResult.Found) {
            throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
          }
        } catch (error: RecordReadException) {
          // The repository removes list/detail only after a confirmed authenticated 404.
          if (error.failure != RecordReadFailure.NOT_FOUND) throw error
        }
      }
      progress = progress.copy(pendingIds = progress.pendingIds.drop(1))
      saveSyncCheckpoint(accountId, progress) // Interrupted writes can replay, never skip unsaved data.
    } else if (progress.cursor == null) {
      // Never prune on a failed/partial pass. An interrupted prune is safe to repeat.
      removeRecords(accountId, progress.removalCandidates.orEmpty())
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
