package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.mobileOpaqueValue
import kotlinx.serialization.Serializable

/** Page-sized work only: no token, profile, question, or unbounded list of all records. */
@Serializable
internal data class RecordSyncCheckpoint(
  val schemaVersion: Int = 1,
  val cursor: String? = null,
  val pendingIds: List<String> = emptyList(),
  val pageLoaded: Boolean = false,
  val cursorHashes: Set<String> = emptySet(),
  val complete: Boolean = false,
) {
  fun validate() {
    check(schemaVersion == 1 && (cursor == null || validRecordCursor(cursor)))
    check(pendingIds.size <= 12 && pendingIds.distinct().size == pendingIds.size &&
      pendingIds.all(mobileOpaqueValue::matches) && cursorHashes.all(mobileOpaqueValue::matches))
    check(pageLoaded || pendingIds.isEmpty())
    check(!complete || (pageLoaded && cursor == null && pendingIds.isEmpty()))
  }

  override fun toString(): String = "RecordSyncCheckpoint(<redacted>)"
}
