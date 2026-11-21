package dev.pitekusu.shittim.records

import java.text.Collator
import java.text.Normalizer
import java.util.Locale

internal val GALLERY_RARITIES = listOf(1, 2, 3, 4, 0)
internal enum class GallerySort { Common, Rare, Newest, Oldest }

internal data class GalleryFilters(
  val query: String = "",
  val rarity: Int? = null,
  val school: String? = null,
  val acquiredOnly: Boolean = false,
  val sort: GallerySort = GallerySort.Common,
) {
  val hasFilters: Boolean
    get() = query.isNotEmpty() || rarity != null || school != null || acquiredOnly

  fun clearFilters(): GalleryFilters = GalleryFilters(sort = sort)

  fun entries(collection: StudentCollection): List<StudentCollectionEntry> {
    val terms = searchTerms()
    val matching = collection.entries.filter {
      (!acquiredOnly || it.cards.isNotEmpty()) && matches(it.studentName, it.schoolName, it.rarity, terms)
    }
    // Keep the roster order for equal rarities and put unacquired students after acquired ones.
    return when (sort) {
      GallerySort.Newest -> matching.sortedBy { it.firstAcquisitionOrder?.let(Int::unaryMinus) ?: Int.MAX_VALUE }
      GallerySort.Oldest -> matching.sortedBy { it.firstAcquisitionOrder ?: Int.MAX_VALUE }
      GallerySort.Common, GallerySort.Rare -> matching.sortedBy { rarityOrder(it.rarity) }
    }
  }

  fun cards(collection: StudentCollection): List<GalleryCard> {
    val terms = searchTerms()
    val matching = collection.cards.filter { matches(it.studentName, it.schoolName, it.rarity, terms) }
    return when (sort) {
      GallerySort.Newest -> matching
      GallerySort.Oldest -> matching.asReversed()
      GallerySort.Common, GallerySort.Rare -> matching.sortedBy { rarityOrder(it.rarity) }
    }
  }

  private fun searchTerms(): List<String> =
    normalizeGalleryName(query).split(galleryWhitespace).filter(String::isNotEmpty)

  private fun matches(name: String?, school: String?, rarity: Int?, terms: List<String>): Boolean =
    (this.rarity == null || this.rarity == rarity) && (this.school == null || this.school == school) &&
      (terms.isEmpty() || terms.all(normalizeGalleryName(name.orEmpty())::contains))

  private fun rarityOrder(rarity: Int?): Int = when (rarity) {
    null, 0 -> Int.MAX_VALUE
    else -> if (sort == GallerySort.Rare) -rarity else rarity
  }
}

internal fun gallerySchoolOptions(collection: StudentCollection, cardsView: Boolean): List<String> {
  val names = if (cardsView) collection.cards.map { it.schoolName }
    else collection.entries.mapNotNull { it.schoolName }
  return names.distinct().sortedWith(Collator.getInstance(Locale.JAPAN))
}

private val galleryWhitespace = Regex("[\\s\\p{Z}\\uFEFF]+")
private fun normalizeGalleryName(value: String): String =
  Normalizer.normalize(value, Normalizer.Form.NFKC).trim().lowercase(Locale.JAPAN)
