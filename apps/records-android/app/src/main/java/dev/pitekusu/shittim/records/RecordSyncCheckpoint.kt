package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.mobileOpaqueValue
import kotlinx.serialization.Serializable

/** No token, profile or question. Removal candidates are encrypted and cleared on completion. */
@Serializable
internal data class RecordSyncCheckpoint(
  val schemaVersion: Int = 1,
  val cursor: String? = null,
  val pendingIds: List<String> = emptyList(),
  val pageLoaded: Boolean = false,
  val cursorHashes: Set<String> = emptySet(),
  val complete: Boolean = false,
  val removalCandidates: Set<String>? = null,
  val indexBased: Boolean = false,
  val pendingReferences: List<RecordSyncReference> = emptyList(),
) {
  fun validate() {
    check(schemaVersion == 1 && (cursor == null || validRecordCursor(cursor)))
    check(pendingIds.size <= (if (indexBased) 50 else 12) && pendingIds.distinct().size == pendingIds.size &&
      pendingIds.all(mobileOpaqueValue::matches) && cursorHashes.all(mobileOpaqueValue::matches))
    check(pageLoaded || pendingIds.isEmpty())
    check(removalCandidates?.all(mobileOpaqueValue::matches) != false)
    check(!complete || (pageLoaded && cursor == null && pendingIds.isEmpty()))
    check(!indexBased || pendingReferences.map { it.recordId } == pendingIds)
    check(pendingReferences.all { mobileOpaqueValue.matches(it.revision) && mobileOpaqueValue.matches(it.avatarRevision) })
  }

  override fun toString(): String = "RecordSyncCheckpoint(<redacted>)"
}
