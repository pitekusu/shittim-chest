package dev.pitekusu.shittim.records

import java.math.BigDecimal
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import kotlinx.serialization.Required
import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder

internal enum class InsightsFailure { AUTH_REQUIRED, UNAVAILABLE, PREPARING, CURSOR_INVALID, INVALID_RESPONSE, STORAGE_UNAVAILABLE }
internal class InsightsException(val failure: InsightsFailure) : Exception("insights_${failure.name.lowercase()}")
internal data class InsightsSaved<T>(val value: T, val fetchedAt: Instant)

@Serializable
internal enum class InsightsPeriod(val wireValue: String) {
  @SerialName("today") Today("today"), @SerialName("week") Week("week"),
  @SerialName("month") Month("month"), @SerialName("all") All("all"),
}

@Serializable
internal data class InsightsCountEntry(
  val rank: Int, val displayName: String,
  @Serializable(with = InsightsAvatarSerializer::class) val avatar: RecordAvatar, val count: Long,
)
@Serializable
internal data class InsightsRankings(
  @Required val schemaVersion: Int = 1, val wins: List<InsightsCountEntry>,
  val requests: List<InsightsCountEntry>, val generatedAt: String,
) {
  fun validate() {
    check(schemaVersion == 1); insightTimestamp(generatedAt)
    check(wins.size <= 3 && requests.size <= 10)
    listOf(wins, requests).forEach { entries ->
      entries.forEach { check(it.rank > 0 && it.count >= 0 && it.displayName.isNotBlank()); validateInsightAvatar(it.avatar) }
      entries.forEachIndexed { index, entry ->
        val previous = entries.getOrNull(index - 1)
        check(entry.rank == if (previous?.count == entry.count) previous.rank else index + 1)
        check(previous == null || entry.count <= previous.count)
      }
    }
  }
}

@Serializable
internal data class InsightsAffectionEntry(
  val rank: Int, val displayName: String,
  @Serializable(with = InsightsAvatarSerializer::class) val avatar: RecordAvatar,
  val score: Int, val resetCount: Int = 0,
)
@Serializable
internal data class InsightsParticipantRanking(
  val participant: String, val displayName: String, val entries: List<InsightsAffectionEntry>,
)
@Serializable
internal data class InsightsAffectionPage(
  @Required val schemaVersion: Int = 1, val generatedAt: String, @Required val defaultScore: Int = 500,
  @Required val maxScore: Int = 1000, val rankings: List<InsightsParticipantRanking>, @Required val nextCursor: String? = null,
) {
  fun validate() {
    validateInsightHeader(schemaVersion, generatedAt, defaultScore, maxScore, nextCursor)
    check(rankings.map { it.participant } == INSIGHTS_PARTICIPANTS)
    check(rankings.map { it.entries.size }.distinct().size == 1)
    rankings.forEach { ranking ->
      check(ranking.displayName.isNotBlank() && ranking.entries.size <= 50)
      ranking.entries.forEachIndexed { index, entry ->
        check(entry.rank > 0 && entry.displayName.isNotBlank() && entry.score in 0..1000 && entry.resetCount >= 0)
        validateInsightAvatar(entry.avatar)
        ranking.entries.getOrNull(index - 1)?.let { previous ->
          check(entry.score <= previous.score && if (entry.score == previous.score) entry.rank == previous.rank else entry.rank > previous.rank)
        }
      }
    }
  }
}
@Serializable
internal data class InsightsRequesterParticipant(
  val participant: String, val displayName: String, val score: Int, val resetCount: Int,
)
@Serializable
internal data class InsightsRequester(
  val displayName: String, @Serializable(with = InsightsAvatarSerializer::class) val avatar: RecordAvatar,
  val participants: List<InsightsRequesterParticipant>,
)
@Serializable
internal data class InsightsRequesterPage(
  @Required val schemaVersion: Int = 1, val generatedAt: String, @Required val defaultScore: Int = 500,
  @Required val maxScore: Int = 1000, val requesters: List<InsightsRequester>, @Required val nextCursor: String? = null,
) {
  fun validate() {
    validateInsightHeader(schemaVersion, generatedAt, defaultScore, maxScore, nextCursor)
    check(requesters.size <= 10)
    requesters.forEach { requester ->
      check(requester.displayName.isNotBlank()); validateInsightAvatar(requester.avatar)
      check(requester.participants.map { it.participant } == INSIGHTS_PARTICIPANTS)
      requester.participants.forEach { check(it.displayName.isNotBlank() && it.score in 0..1000 && it.resetCount >= 0) }
    }
  }
}

@Serializable
internal data class InsightsCostBreakdown(
  @Serializable(with = InsightsJpySerializer::class) val fargate: BigDecimal,
  @Serializable(with = InsightsJpySerializer::class) val lambda: BigDecimal,
  @Serializable(with = InsightsJpySerializer::class) val openai: BigDecimal,
  @Serializable(with = InsightsJpySerializer::class) val otherAws: BigDecimal,
)
@Serializable
internal data class InsightsCostConversion(
  val source: String, val method: String, val baseCurrency: String, val updatedAt: String?,
)
@Serializable
internal data class InsightsCosts(
  @Required val schemaVersion: Int = 1, val period: InsightsPeriod, val timeZone: String,
  val startDate: String, val endDate: String, val currency: String,
  @Serializable(with = InsightsJpySerializer::class) val total: BigDecimal,
  val breakdown: InsightsCostBreakdown, val conversion: InsightsCostConversion,
  val updatedAt: String?, val status: String,
) {
  fun validate() {
    check(schemaVersion == 1 && timeZone == "Asia/Tokyo" && currency == "JPY")
    check(LocalDate.parse(startDate) <= LocalDate.parse(endDate))
    check(status in setOf("partial", "final", "unavailable"))
    check(conversion.source == "frankfurter-v2" && conversion.method == "daily-reference-rate" && conversion.baseCurrency == "USD")
    conversion.updatedAt?.let(::insightTimestamp); updatedAt?.let(::insightTimestamp)
    listOf(total, breakdown.fargate, breakdown.lambda, breakdown.openai, breakdown.otherAws).forEach {
      check(it.signum() >= 0 && it.scale() == 6)
    }
  }
}

internal val INSIGHTS_PARTICIPANTS = listOf("participant-a", "participant-b", "participant-c")
internal fun insightTimestamp(value: String): Instant = OffsetDateTime.parse(value).toInstant()
internal fun validInsightCursor(value: String): Boolean = value.length in 1..4096 && value.none { it.isISOControl() }
private fun validateInsightHeader(schema: Int, date: String, default: Int, max: Int, cursor: String?) {
  check(schema == 1 && default == 500 && max == 1000); insightTimestamp(date)
  check(cursor == null || validInsightCursor(cursor))
}
private fun validateInsightAvatar(avatar: RecordAvatar) {
  check(avatar.fallbackVariant in setOf("cyan", "pink", "lavender"))
  check(avatar.url == null || validStoredAvatarUrl(avatar.url, allowParticipants = true))
}

/** API and encrypted pages share validation; only the UI-owned bytes are transient. */
internal object InsightsAvatarSerializer : KSerializer<RecordAvatar> {
  override val descriptor = InsightsAvatarRef.serializer().descriptor
  override fun serialize(encoder: Encoder, value: RecordAvatar) {
    validateInsightAvatar(value)
    encoder.encodeSerializableValue(InsightsAvatarRef.serializer(), InsightsAvatarRef(
      if (value.url == null) "placeholder" else "image", value.url, "アバター", value.fallbackVariant))
  }
  override fun deserialize(decoder: Decoder): RecordAvatar {
    val ref = decoder.decodeSerializableValue(InsightsAvatarRef.serializer())
    check(ref.alt.isNotBlank() && ref.kind in setOf("placeholder", "image") && (ref.kind == "image") == (ref.url != null))
    return RecordAvatar(ref.url, ref.fallbackVariant).also(::validateInsightAvatar)
  }
}
@Serializable
private data class InsightsAvatarRef(val kind: String, val url: String? = null, val alt: String, val fallbackVariant: String)
internal object InsightsJpySerializer : KSerializer<BigDecimal> {
  override val descriptor = PrimitiveSerialDescriptor("InsightsJpy", PrimitiveKind.STRING)
  override fun serialize(encoder: Encoder, value: BigDecimal) {
    check(value.signum() >= 0 && value.scale() == 6); encoder.encodeString(value.toPlainString())
  }
  override fun deserialize(decoder: Decoder): BigDecimal {
    val raw = decoder.decodeString(); check(Regex("[0-9]+\\.[0-9]{6}").matches(raw))
    return BigDecimal(raw)
  }
}

/** Global ranks and metadata are checked across page boundaries before a manifest is committed. */
internal fun validateParticipantPages(pages: List<InsightsAffectionPage>) {
  check(pages.isNotEmpty())
  val first = pages.first()
  pages.forEach { page ->
    page.validate()
    check(page.generatedAt == first.generatedAt && page.rankings.map { it.displayName } == first.rankings.map { it.displayName })
  }
  INSIGHTS_PARTICIPANTS.indices.forEach { slot ->
    val entries = pages.flatMap { it.rankings[slot].entries }
    entries.forEachIndexed { index, entry ->
      val previous = entries.getOrNull(index - 1)
      check(entry.rank == if (previous?.score == entry.score) previous.rank else index + 1)
      check(previous == null || entry.score <= previous.score)
    }
  }
  check(pages.dropLast(1).all { it.nextCursor != null })
  check(pages.mapNotNull { it.nextCursor }.distinct().size == pages.mapNotNull { it.nextCursor }.size)
}
internal fun validateRequesterPages(pages: List<InsightsRequesterPage>) {
  check(pages.isNotEmpty())
  pages.forEach { it.validate(); check(it.generatedAt == pages.first().generatedAt) }
  check(pages.dropLast(1).all { it.nextCursor != null })
  check(pages.mapNotNull { it.nextCursor }.distinct().size == pages.mapNotNull { it.nextCursor }.size)
}
