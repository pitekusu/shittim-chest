package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.mobileOpaqueValue
import java.io.Closeable
import java.time.Instant
import java.time.OffsetDateTime
import java.util.Base64
import kotlinx.serialization.Serializable

internal enum class GalleryReadFailure { AUTH_REQUIRED, NOT_FOUND, UNAVAILABLE, INVALID_RESPONSE }

internal class GalleryReadException(
  val failure: GalleryReadFailure,
  val serverRejectedSession: Boolean = false,
) : Exception("gallery_read_${failure.name.lowercase()}")

internal interface StudentGallery : Closeable {
  suspend fun loadCollection(): StudentCollection
  suspend fun loadThumbnails(ids: List<String>): Map<String, ByteArray>
  suspend fun loadOriginal(id: String): ByteArray
}

internal data class StudentCollection(
  val entries: List<StudentCollectionEntry>,
  val cards: List<GalleryCard>,
)

internal data class StudentCollectionEntry(
  val studentId: String,
  val studentName: String?,
  val schoolName: String?,
  val rarity: Int,
  val cards: List<GalleryCard>,
  val firstAcquisitionOrder: Int?,
) {
  override fun toString(): String = "StudentCollectionEntry(<redacted>)"
}

internal data class GalleryCard(
  val cardId: String,
  val studentId: String,
  val studentName: String,
  val schoolName: String,
  val rarity: Int?,
  val postedAt: Instant,
  val performer: String,
  val collectorName: String?,
  val isMine: Boolean,
  val thumbnailReady: Boolean,
) {
  override fun toString(): String = "GalleryCard(<redacted>)"
}

internal const val GALLERY_COLLECTION_CONTRACT = "gallery-collection-v1"
internal const val GALLERY_THUMBNAIL_BATCH_SIZE = 24
private val galleryCardId = Regex("[A-Za-z0-9_-]{80}")
private val galleryResultKey = Regex("[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\\.[0-9]{6}Z#([a-f0-9]{32})")
private val galleryPerformers = setOf("participant-a", "participant-b", "participant-c")

internal fun galleryDrawId(cardId: String): String? {
  if (!galleryCardId.matches(cardId)) return null
  // Gallery card IDs encode the immutable storage position. Its draw ID binds signed media to
  // the requested card, independently of the URL's signature and the API's ticket association.
  val storageKey = String(Base64.getUrlDecoder().decode(cardId), Charsets.US_ASCII)
  return galleryResultKey.matchEntire(storageKey)?.groupValues?.get(1)
}

@Serializable
internal class GalleryRosterEntry(
  val studentId: String,
  val studentName: String?,
  val schoolName: String?,
  val rarity: Int,
) {
  fun validate() {
    check(mobileOpaqueValue.matches(studentId) && rarity in 0..4)
    if (rarity == 0 || rarity == 4) {
      check(studentName == null && schoolName == null)
    } else {
      check(studentName != null && studentName.validGalleryName(60) &&
        schoolName != null && schoolName.validGalleryName(40))
    }
  }
}

@Serializable
internal class GalleryRosterResponse(val schemaVersion: Int, val items: List<GalleryRosterEntry>) {
  fun validate() {
    check(schemaVersion == 1 && items.size in 1..2000)
    check(items.map { it.studentId }.toSet().size == items.size)
    items.forEach { it.validate() }
  }
}

@Serializable
internal class GalleryCardPage(
  val schemaVersion: Int,
  val contract: String,
  val items: List<GalleryCardResponse>,
  val nextCursor: String?,
) {
  fun validate() {
    check(schemaVersion == 1 && contract == GALLERY_COLLECTION_CONTRACT && items.size <= 100)
    check(nextCursor == null || (nextCursor.length <= 256 && validRecordCursor(nextCursor)))
  }
}

@Serializable
internal class GalleryCardResponse(
  val cardId: String,
  val studentId: String,
  val studentName: String,
  val schoolName: String,
  val rarity: Int?,
  val postedAt: String,
  val performer: String,
  val collector: GalleryCollector?,
  val isMine: Boolean,
  val thumbnailReady: Boolean,
) {
  fun toCard(): GalleryCard {
    check(galleryDrawId(cardId) != null && mobileOpaqueValue.matches(studentId))
    check(studentName.validGalleryName(60) && schoolName.validGalleryName(40) &&
      (rarity == null || rarity in 0..4) && performer in galleryPerformers)
    check(collector == null || (mobileOpaqueValue.matches(collector.id) && collector.displayName.isNotBlank()))
    return GalleryCard(cardId, studentId, studentName, schoolName, rarity,
      OffsetDateTime.parse(postedAt).toInstant(), performer, collector?.displayName, isMine, thumbnailReady)
  }
}

@Serializable
internal class GalleryCollector(val id: String, val displayName: String)

private fun String.validGalleryName(maxLength: Int): Boolean =
  isNotBlank() && codePointCount(0, length) <= maxLength

internal fun combineStudentCollection(
  roster: List<GalleryRosterEntry>,
  newestFirst: List<GalleryCard>,
): StudentCollection {
  // Keep the server's storage order, including when two cards have the same timestamp.
  val cardsByStudent = newestFirst.asReversed().groupBy { it.studentId }
  val acquisitionOrder = cardsByStudent.keys.withIndex().associate { it.value to it.index }
  return StudentCollection(roster.map { entry ->
    val cards = cardsByStudent[entry.studentId].orEmpty()
    val first = cards.firstOrNull()
    StudentCollectionEntry(entry.studentId, entry.studentName ?: first?.studentName,
      entry.schoolName ?: first?.schoolName, entry.rarity, cards, acquisitionOrder[entry.studentId])
  }, newestFirst)
}
