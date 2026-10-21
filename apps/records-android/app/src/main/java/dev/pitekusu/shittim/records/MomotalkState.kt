package dev.pitekusu.shittim.records

internal data class MomotalkResource<T>(
  val data: T? = null,
  val loading: Boolean = false,
  val error: MomotalkFailure? = null,
  val saved: Boolean = false,
  val revision: Long = 0L,
)

/** Decrypted conversations and image bytes remain inside the authorized runtime. */
internal data class MomotalkUiState(
  val selectedWeek: String? = null,
  val selectedRoom: String? = null,
  val online: Boolean = false,
  val weeks: MomotalkResource<List<MomotalkWeeksPage>> = MomotalkResource(),
  val rooms: MomotalkResource<List<MomotalkRoomsPage>> = MomotalkResource(),
  val detail: MomotalkResource<MomotalkRoomDetail> = MomotalkResource(),
)

internal sealed interface MomotalkEvent {
  data class SelectWeek(val value: String) : MomotalkEvent
  data class SelectRoom(val value: String) : MomotalkEvent
  data object MoreWeeks : MomotalkEvent
  data object MoreRooms : MomotalkEvent
  data object Refresh : MomotalkEvent
  data object Back : MomotalkEvent
}

/** Only navigation identifiers may survive a fresh verification. */
internal data class MomotalkSelection(val selectedWeek: String? = null, val selectedRoom: String? = null)
internal fun MomotalkUiState.selection() = MomotalkSelection(selectedWeek, selectedRoom)
