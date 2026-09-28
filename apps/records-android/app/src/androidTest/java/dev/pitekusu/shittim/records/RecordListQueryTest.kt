package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.ui.participantVisualSlot
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RecordListQueryTest {
  private fun entry(id: String, date: String, winner: String = "アロナ") = RecordListEntry(
    id.repeat(43), "架空の料理相談", "架空の依頼者", RecordAvatar(null, "cyan"), Instant.parse(date), winner)

  @Test fun orderIsDeterministicAndWinnerUsesStoredResult() {
    val a = entry("a", "2026-09-24T00:00:00Z")
    val b = entry("b", "2026-09-26T00:00:00Z", "プラナ")
    val c = entry("c", "2026-09-26T00:00:00Z", "安倍晋三AI")
    val list = listOf(c, a, b)
    assertEquals(listOf(b, c, a), RecordListQuery().sorted(list))
    assertEquals(listOf(a, b, c), RecordListQuery(order = RecordOrder.Oldest).sorted(list))
    assertEquals(listOf(b), list.filter(RecordListQuery(winner = RecordWinner.Plana)::acceptsWinner))
    assertEquals(listOf(c), list.filter(RecordListQuery(winner = RecordWinner.Abe)::acceptsWinner))
  }

  @Test fun missingDetailStillMatchesMetadataAndBlankQueryDoesNotSearchBody() {
    val item = entry("a", "2026-09-24T00:00:00Z")
    assertTrue(RecordListQuery("料理　依頼者").matches(item))
    assertFalse(RecordListQuery("料理 旅行").matches(item))
    assertFalse(RecordListQuery("ＡＡＡ").matches(item)) // Internal IDs are not searchable text.
    assertFalse(RecordListQuery("　 ").searchesText)
    assertTrue(RecordListQuery("　 ").isDefault)
  }

  @Test fun participantSlotSurvivesNameChangesAndOldCacheUsesKnownNames() {
    val stored = RecordListEntry("a".repeat(43), "架空の相談", "依頼者", RecordAvatar(null, "cyan"),
      Instant.parse("2026-09-24T00:00:00Z"), "プラナの新しい名前", "participant-b")
    assertTrue(RecordListQuery(winner = RecordWinner.Plana).acceptsWinner(stored))
    assertEquals("participant-b", participantVisualSlot(stored.winnerName, stored.winnerSlot))
    assertEquals("participant-a", participantVisualSlot("アロナ", null))
    assertEquals(null, participantVisualSlot("不明な人格", null))
  }
}
