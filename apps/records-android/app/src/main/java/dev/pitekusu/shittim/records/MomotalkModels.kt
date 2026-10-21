package dev.pitekusu.shittim.records

import java.net.URI
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import kotlinx.serialization.Required
import kotlinx.serialization.Serializable
import kotlinx.serialization.Transient

internal enum class MomotalkFailure { AUTH_REQUIRED, NOT_FOUND, UNAVAILABLE, CURSOR_INVALID, INVALID_RESPONSE, STORAGE_UNAVAILABLE }
internal class MomotalkException(val failure: MomotalkFailure) : Exception("momotalk_${failure.name.lowercase()}")
internal data class MomotalkSaved<T>(val value: T, val fetchedAt: Instant)
internal val MOMOTALK_PARTICIPANTS = listOf("participant-a", "participant-b", "participant-c")
internal fun validMomotalkWeekId(value: String): Boolean = runCatching {
  Regex("[0-9]{4}-[0-9]{2}-[0-9]{2}").matches(value) && LocalDate.parse(value).toString() == value
}.getOrDefault(false)
internal fun validMomotalkRoomId(value: String): Boolean = Regex("[A-Za-z0-9_-]{43}").matches(value)

@Serializable
internal data class MomotalkWeek(val weekId: String, val periodStart: String, val periodEnd: String, val publishAt: String) {
  fun validate() {
    check(validMomotalkWeekId(weekId))
    val start = OffsetDateTime.parse(periodStart).toInstant()
    val end = OffsetDateTime.parse(periodEnd).toInstant()
    check(start < end && end <= OffsetDateTime.parse(publishAt).toInstant())
  }
}
@Serializable
internal data class MomotalkWeeksPage(@Required val schemaVersion: Int = 1,
  val weeks: List<MomotalkWeek>, @Required val nextCursor: String? = null) {
  fun validate() {
    check(schemaVersion == 1 && weeks.size <= 20)
    weeks.forEach { it.validate() }
    check(weeks.map { it.weekId }.distinct().size == weeks.size)
    check(weeks.zipWithNext().all { (a, b) -> a.weekId > b.weekId })
    check(nextCursor == null || validMomotalkWeekId(nextCursor))
  }
}
@Serializable
internal data class MomotalkRequester(val displayName: String,
  @Serializable(with = InsightsAvatarSerializer::class) val avatar: RecordAvatar)
@Serializable
internal data class MomotalkRoomSummary(val roomId: String, val requester: MomotalkRequester,
  val questionCount: Int, val state: String) {
  fun validate() {
    check(validMomotalkRoomId(roomId) && questionCount >= 0 && state in setOf("preparing", "ready", "failed"))
    check(requester.displayName.isNotBlank())
    validateMomotalkAvatar(requester.avatar)
  }
}
@Serializable
internal data class MomotalkRoomsPage(@Required val schemaVersion: Int = 1, val week: MomotalkWeek,
  val rooms: List<MomotalkRoomSummary>, @Required val nextCursor: String? = null) {
  fun validate() {
    check(schemaVersion == 1 && rooms.size <= 20); week.validate(); rooms.forEach { it.validate() }
    check(rooms.map { it.roomId }.distinct().size == rooms.size)
    check(rooms.zipWithNext().all { (a, b) -> a.roomId < b.roomId })
    check(nextCursor == null || validMomotalkRoomId(nextCursor))
  }
}
@Serializable
internal data class MomotalkParticipant(val slot: String, val displayName: String,
  @Serializable(with = InsightsAvatarSerializer::class) val avatar: RecordAvatar)
@Serializable
internal data class MomotalkMessage(val id: Int, val participant: String, val text: String)
@Serializable
internal data class MomotalkImageSubject(val participant: String, val mood: String)
@Serializable
internal data class MomotalkImage(val participant: String, val mood: String, val subjects: List<MomotalkImageSubject>,
  val state: String, val url: String? = null, val thumbnailUrl: String? = null, val downloadUrl: String? = null,
  @Transient val bytes: ByteArray? = null) {
  val key: String get() = "$participant-$mood"
  fun cached() = copy(url = null, thumbnailUrl = null, downloadUrl = null, bytes = null)
  override fun toString(): String = "MomotalkImage(<redacted>)"
}
@Serializable
internal data class MomotalkRoomDetail(@Required val schemaVersion: Int = 1, val week: MomotalkWeek,
  val room: MomotalkRoomSummary, val participants: List<MomotalkParticipant>,
  val messages: List<MomotalkMessage>, val images: List<MomotalkImage>) {
  fun validate(requireUrls: Boolean = false) {
    check(schemaVersion == 1); week.validate(); room.validate()
    check(participants.map { it.slot }.toSet() == MOMOTALK_PARTICIPANTS.toSet() && participants.size == 3)
    participants.forEach { check(it.displayName.isNotBlank()); validateMomotalkAvatar(it.avatar) }
    check(messages.size <= 15 && messages.map { it.id } == (1..messages.size).toList())
    check(messages.all { it.participant in MOMOTALK_PARTICIPANTS && it.text.isNotBlank() })
    check(images.size <= 2 && images.map { it.key }.distinct().size == images.size)
    check(room.state == "ready" || (messages.isEmpty() && images.isEmpty()))
    images.forEach { image ->
      check(image.participant in MOMOTALK_PARTICIPANTS && image.mood in setOf("happy", "unhappy"))
      check(image.state in setOf("pending", "ready", "failed"))
      check(image.subjects.size in 1..3 && image.subjects.map { it.participant }.distinct().size == image.subjects.size)
      check(image.subjects.first() == MomotalkImageSubject(image.participant, image.mood))
      check(image.subjects.all { it.participant in MOMOTALK_PARTICIPANTS && it.mood in setOf("happy", "unhappy") })
      val urls = listOf(image.url, image.thumbnailUrl, image.downloadUrl)
      if (image.state == "ready") {
        check(!requireUrls || urls.all { it != null })
        urls.filterNotNull().forEach { check(validMomotalkImageUrl(it, week.weekId, room.roomId)) }
      } else check(urls.all { it == null })
    }
  }
  fun cached() = copy(room = room.copy(requester = room.requester.copy(avatar = room.requester.avatar.withoutUrl())),
    participants = participants.map { it.copy(avatar = it.avatar.withoutUrl()) }, images = images.map { it.cached() })
}
internal fun RecordAvatar.withoutUrl() = RecordAvatar(null, fallbackVariant)
private fun validateMomotalkAvatar(avatar: RecordAvatar) {
  check(avatar.fallbackVariant in setOf("cyan", "pink", "lavender"))
  check(avatar.url == null || validStoredAvatarUrl(avatar.url, allowParticipants = true))
}

/** Reuse the attachment bucket boundary, with the exact published MomoTalk image namespace. */
internal fun validMomotalkImageUrl(raw: String, weekId: String, roomId: String): Boolean = try {
  val uri = URI(raw)
  validMomotalkWeekId(weekId) && validMomotalkRoomId(roomId) && raw.length <= 8192 &&
    uri.scheme == "https" && uri.port in listOf(-1, 443) && uri.userInfo == null && uri.fragment == null &&
    Regex("shittim-chest-production-records-media-[0-9]{12}\\.s3\\.[a-z0-9-]+\\.amazonaws\\.com").matches(uri.host.orEmpty()) &&
    uri.normalize().rawPath == uri.rawPath &&
    uri.rawPath.matches(Regex("/momotalk/images/$weekId/$roomId/(selfie|happy|unhappy)(-thumb)?\\.webp"))
} catch (_: Exception) { false }
