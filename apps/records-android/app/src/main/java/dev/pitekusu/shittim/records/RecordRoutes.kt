package dev.pitekusu.shittim.records

import androidx.navigation3.runtime.NavBackStack
import androidx.navigation3.runtime.NavKey
import dev.pitekusu.shittim.records.auth.mobileOpaqueValue
import kotlinx.serialization.Serializable

// Route hints never grant access. Do not add account identity, text, or credentials here.
@Serializable
internal data object RecordsList : NavKey

@Serializable
internal data object DebateCompose : NavKey

@Serializable
internal data class RecordDetail(val recordId: String) : NavKey {
  init { require(mobileOpaqueValue.matches(recordId)) { "invalid_record_route" } }
}

// Preserve the existing list + one detail behavior, including links received in two-pane mode.
internal fun NavBackStack<NavKey>.openRecord(recordId: String) {
  val detail = RecordDetail(recordId)
  if (lastOrNull() == detail) return
  if (size > 1) this[lastIndex] = detail else add(detail)
}

internal fun NavBackStack<NavKey>.closeRecord() {
  while (size > 1) removeLastOrNull()
}
