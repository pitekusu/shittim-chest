package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.ui.participantVisualSlot
import java.time.Instant
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RecordListQueryTest {
  private fun entry(id: String, date: String, winner: String = "アロナ",
    requester: String = "架空の依頼者", avatar: RecordAvatar = RecordAvatar(null, "cyan")) = RecordListEntry(
    id.repeat(43), "架空の料理相談", requester, avatar, Instant.parse(date), winner)

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

  @Test fun requesterFilterMatchesTheWholeDisplayNameAndNullIncludesEveryone() {
    val date = "2026-09-24T00:00:00Z"
    val entries = listOf(
      entry("a", date, requester = "架空Alice"),
      entry("b", date, winner = "プラナ", requester = "架空Alice"),
      entry("c", date, requester = "架空Alice2"),
      entry("d", date, requester = "架空alice"),
      entry("e", date, requester = "架空Ａｌｉｃｅ"),
      entry("f", date, requester = "架空Alice "),
    )
    val selected = RecordListQuery(requesterName = "架空Alice")
    assertEquals(entries.take(2), entries.filter(selected::acceptsRequester))
    assertTrue(entries.all(RecordListQuery()::acceptsRequester))
    assertFalse(selected.isDefault)
    assertTrue(selected.copy(requesterName = null).isDefault)
    assertTrue(entries.none(RecordListQuery(requesterName = "存在しない架空の依頼者")::acceptsRequester))
  }

  @Test fun requesterChoicesUseTheWholeSnapshotAndDeduplicateNamesWithTheNewestSavedImage() {
    val requester = "架空Alice"
    val oldIcon = RecordAvatar(null, "cyan", bytes = byteArrayOf(1))
    val newestSavedIcon = RecordAvatar(null, "lavender", bytes = byteArrayOf(2, 3))
    val tiedIcon = RecordAvatar(null, "pink", bytes = byteArrayOf(4))
    val entries = listOf(
      entry("d", "2026-09-27T00:00:00Z", requester = requester, avatar = RecordAvatar(null, "cyan")),
      entry("c", "2026-09-26T00:00:00Z", requester = requester, avatar = tiedIcon),
      entry("a", "2026-09-24T00:00:00Z", requester = requester, avatar = oldIcon),
      entry("f", "2026-09-27T00:00:00Z", winner = "プラナ", requester = "架空Bob",
        avatar = RecordAvatar(null, "pink")),
      entry("b", "2026-09-26T00:00:00Z", requester = requester, avatar = newestSavedIcon),
      entry("e", "2026-09-24T00:00:00Z", winner = "プラナ", requester = "架空Bob"),
    )
    // A selected winner/requester may hide Bob's records, but must not remove his filter choice.
    val filtered = entries.filter { RecordListQuery(winner = RecordWinner.Arona,
      requesterName = requester).let { query -> query.acceptsWinner(it) && query.acceptsRequester(it) } }
    assertTrue(filtered.none { it.requesterName == "架空Bob" })
    for (snapshot in listOf(entries, entries.reversed())) {
      val allChoices = recordRequesterChoices(snapshot)
      assertEquals(2, allChoices.size)
      val choices = allChoices.associateBy { it.displayName }
      assertEquals(setOf(requester, "架空Bob"), choices.keys)
      val alice = choices.getValue(requester).avatar
      assertArrayEquals(newestSavedIcon.bytes, alice.bytes)
      assertEquals("lavender", alice.fallbackVariant)
      val bob = choices.getValue("架空Bob").avatar
      assertNull(bob.bytes)
      assertEquals("pink", bob.fallbackVariant)
    }
    assertTrue(recordRequesterChoices(emptyList()).isEmpty())
  }
}
