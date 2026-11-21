package dev.pitekusu.shittim.records

import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class GalleryFiltersTest {
  private val first = card("first", "initial", "ABC（冬）", "ア学園", 1)
  private val recent = card("recent", "latest", "あかね", "イ学園", 1)
  private val legacy = card("legacy", "retired", "旧名簿", "ウ学園", null)
  private val hidden = StudentCollectionEntry("hidden", null, null, 0, emptyList(), null)
  private val unacquired = StudentCollectionEntry("other", "いろは", "イ学園", 4, emptyList(), null)
  private val initial = entry(first, acquisitionOrder = 0)
  private val latest = entry(recent, acquisitionOrder = 1)
  private val collection = StudentCollection(listOf(latest, hidden, unacquired, initial), listOf(recent, legacy, first))

  @Test fun normalizesNameSearchAndCombinesFiltersWithoutRevealingLockedEntries() {
    val filters = GalleryFilters(query = "　（冬）　ＡＢＣ ", rarity = 1, school = "ア学園", acquiredOnly = true)
    assertEquals(listOf(initial), filters.entries(collection))
    assertEquals(listOf(first), filters.cards(collection))
    assertEquals(emptyList<StudentCollectionEntry>(), filters.copy(school = "イ学園").entries(collection))
    assertEquals(listOf(hidden), GalleryFilters(rarity = 0).entries(collection))
    assertEquals(emptyList<StudentCollectionEntry>(), GalleryFilters(query = "隠し", rarity = 0).entries(collection))
  }

  @Test fun stableRarityAndAcquisitionOrdersMatchTheCollectionAndRetainLegacyCards() {
    assertEquals(listOf(latest, initial, unacquired, hidden), GalleryFilters().entries(collection))
    assertEquals(listOf(unacquired, latest, initial, hidden), GalleryFilters(sort = GallerySort.Rare).entries(collection))
    assertEquals(listOf(initial, latest, hidden, unacquired), GalleryFilters(sort = GallerySort.Oldest).entries(collection))
    assertEquals(listOf(latest, initial, hidden, unacquired), GalleryFilters(sort = GallerySort.Newest).entries(collection))
    assertEquals(listOf(recent, legacy, first), GalleryFilters(sort = GallerySort.Newest).cards(collection))
    assertEquals(listOf(first, legacy, recent), GalleryFilters(sort = GallerySort.Oldest).cards(collection))
    assertEquals(listOf(recent, first, legacy), GalleryFilters().cards(collection))
    assertEquals(listOf("ア学園", "イ学園"), gallerySchoolOptions(collection, cardsView = false))
    assertEquals(listOf("ア学園", "イ学園", "ウ学園"), gallerySchoolOptions(collection, cardsView = true))
  }

  @Test fun clearingConditionsPreservesTheSelectedSort() {
    val filters = GalleryFilters("あかね", 1, "イ学園", true, GallerySort.Oldest)
    assertTrue(filters.hasFilters)
    val cleared = filters.clearFilters()
    assertFalse(cleared.hasFilters)
    assertEquals(GallerySort.Oldest, cleared.sort)
  }

  private fun card(cardId: String, studentId: String, name: String, school: String, rarity: Int?) =
    GalleryCard(cardId, studentId, name, school, rarity, Instant.EPOCH, "participant-a", null, false, true)

  private fun entry(card: GalleryCard, acquisitionOrder: Int) =
    StudentCollectionEntry(card.studentId, card.studentName, card.schoolName, requireNotNull(card.rarity),
      listOf(card), acquisitionOrder)
}
