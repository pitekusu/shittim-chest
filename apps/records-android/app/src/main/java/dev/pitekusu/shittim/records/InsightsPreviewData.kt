package dev.pitekusu.shittim.records

import java.math.BigDecimal

/** Entirely fictional, network-free data shared by Compose previews and the isolated review demo. */
internal fun insightsSampleState(): InsightsUiState {
  val date = "2026-10-18T00:00:00Z"
  val personas = listOf("アロナ", "プラナ", "安倍晋三AI")
  val variants = listOf("cyan", "pink", "lavender")
  fun avatar(index: Int) = RecordAvatar(null, variants[index % variants.size])
  val names = listOf("架空の依頼者01", "架空の依頼者02", "架空の依頼者02", "架空の依頼者04",
    "架空の依頼者05", "架空の依頼者06", "架空の依頼者07", "架空の依頼者08",
    "架空のとても長い名前を使う依頼者09", "架空の依頼者10")
  val scores = listOf(1000, 820, 820, 700, 600, 500, 400, 300, 100, 0)
  val affection = InsightsAffectionPage(generatedAt = date,
    rankings = INSIGHTS_PARTICIPANTS.mapIndexed { persona, slot ->
      InsightsParticipantRanking(slot, personas[persona], scores.mapIndexed { index, score ->
        InsightsAffectionEntry(scores.indexOf(score) + 1, names[index], avatar(index), score,
          resetCount = if (index == 0) persona + 1 else 0)
      })
    })
  val requesterNames = List(12) { index -> "架空の依頼者${(index + 1).toString().padStart(2, '0')}" }
  val requesterPages = requesterNames.chunked(10).mapIndexed { pageIndex, pageNames ->
    InsightsRequesterPage(generatedAt = date, requesters = pageNames.mapIndexed { index, name ->
      InsightsRequester(name, avatar(index), INSIGHTS_PARTICIPANTS.mapIndexed { persona, slot ->
        InsightsRequesterParticipant(slot, personas[persona],
          when { index == 0 && persona != 2 -> 1000; index == 1 && persona == 2 -> 0
            else -> (820 - index * 40 - persona * 70).coerceAtLeast(0) },
          resetCount = if (index == 0) persona + 1 else 0)
      })
    }, nextCursor = if (pageIndex == 0) "fictional-next-page" else null)
  }
  val counts = listOf(30L, 25L, 20L, 20L, 15L, 10L, 8L, 5L, 3L, 0L)
  val rankings = InsightsRankings(generatedAt = date,
    wins = listOf(12L, 8L, 8L).mapIndexed { index, count ->
      InsightsCountEntry(if (index == 0) 1 else 2, personas[index], avatar(index), count)
    }, requests = counts.mapIndexed { index, count ->
      InsightsCountEntry(counts.indexOf(count) + 1, names[index], avatar(index), count)
    })
  val costs = InsightsPeriod.entries.associateWith { period ->
    val multiple = when (period) {
      InsightsPeriod.Today -> 1; InsightsPeriod.Week -> 7; InsightsPeriod.Month -> 18; InsightsPeriod.All -> 50
    }.toBigDecimal()
    InsightsResource(data = InsightsCosts(period = period, timeZone = "Asia/Tokyo", currency = "JPY",
      startDate = when (period) {
        InsightsPeriod.Today -> "2026-10-18"; InsightsPeriod.Week -> "2026-10-12"
        InsightsPeriod.Month -> "2026-10-01"; InsightsPeriod.All -> "2026-09-01"
      }, endDate = "2026-10-18", total = BigDecimal("130.000002") * multiple,
      breakdown = InsightsCostBreakdown(BigDecimal("18.000001") * multiple, BigDecimal("7.100000") * multiple,
        BigDecimal("100.000001") * multiple, BigDecimal("4.900000") * multiple),
      conversion = InsightsCostConversion("frankfurter-v2", "daily-reference-rate", "USD", date),
      updatedAt = date, status = if (period == InsightsPeriod.Today) "partial" else "final"))
  }
  return InsightsUiState(online = true, rankings = InsightsResource(rankings),
    participants = InsightsResource(listOf(affection)), requesters = InsightsResource(requesterPages), costs = costs)
}
