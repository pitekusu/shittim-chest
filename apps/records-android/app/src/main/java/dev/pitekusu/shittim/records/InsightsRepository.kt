package dev.pitekusu.shittim.records

import android.content.Context
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordCacheException
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import java.io.Closeable
import java.time.Instant
import java.util.Base64
import java.util.UUID
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/** Per-page ciphertext is committed by a small manifest only after its first page is durable. */
internal class InsightsRepository(
  private val remote: RecordsReadClient,
  private val cache: EncryptedRecordStore,
  private val database: EncryptedRecordsDatabase,
  private val account: RecordCacheAccount,
  private val isActiveAccount: (String) -> Boolean,
) : Closeable {
  private val json = Json { encodeDefaults = true }
  @Volatile private var closed = false

  suspend fun cachedRankings(accountId: String): InsightsSaved<InsightsRankings>? = locked(accountId) {
    load<InsightsRankings>(accountId, RANKINGS_ID, CachedRecordPart.INSIGHTS_RANKINGS) { it.validate() }
      ?.let { saved -> saved.copyValue(restoreRankings(accountId, saved.value)) }
  }

  suspend fun refreshRankings(token: String, accountId: String): InsightsSaved<InsightsRankings> {
    locked(accountId) { Unit }
    val value = remote.insightsRankings(token)
    requireActive(accountId)
    return locked(accountId) {
      val saved = save(accountId, RANKINGS_ID, CachedRecordPart.INSIGHTS_RANKINGS, value) { it.validate() }
      pruneAvatars(accountId)
      saved.copyValue(restoreRankings(accountId, value))
    }
  }

  suspend fun cachedCosts(accountId: String, period: InsightsPeriod): InsightsSaved<InsightsCosts>? = locked(accountId) {
    load<InsightsCosts>(accountId, costsId(period), CachedRecordPart.INSIGHTS_COSTS) { it.validate(); check(it.period == period) }
  }

  suspend fun refreshCosts(token: String, accountId: String, period: InsightsPeriod): InsightsSaved<InsightsCosts> {
    locked(accountId) { Unit }
    val value = remote.insightsCosts(token, period)
    requireActive(accountId)
    return locked(accountId) {
      save(accountId, costsId(period), CachedRecordPart.INSIGHTS_COSTS, value) { it.validate(); check(it.period == period) }
    }
  }

  suspend fun cachedParticipants(accountId: String): List<InsightsSaved<InsightsAffectionPage>> = locked(accountId) {
    participantPages(accountId).map { saved -> saved.copyValue(restoreParticipants(accountId, saved.value)) }
  }

  suspend fun refreshParticipants(token: String, accountId: String): List<InsightsSaved<InsightsAffectionPage>> {
    locked(accountId) { Unit }
    val page = remote.insightsParticipants(token)
    requireActive(accountId)
    validateRemote { validateParticipantPages(listOf(page)) }
    return locked(accountId) {
      val generation = UUID.randomUUID().toString()
      val id = "insights-participants-$generation-0"
      val saved = save(accountId, id, CachedRecordPart.INSIGHTS_AFFECTION, page) { it.validate() }
      saveManifest(accountId, CachedRecordPart.INSIGHTS_AFFECTION, PARTICIPANTS_MANIFEST,
        InsightManifest(1, generation, page.generatedAt, listOf(id)))
      prunePages(accountId, CachedRecordPart.INSIGHTS_AFFECTION, PARTICIPANTS_MANIFEST, setOf(id))
      pruneAvatars(accountId)
      listOf(saved.copyValue(restoreParticipants(accountId, page)))
    }
  }

  suspend fun appendParticipants(token: String, accountId: String, cursor: String): List<InsightsSaved<InsightsAffectionPage>> {
    val previous = locked(accountId) {
      val manifest = manifest(accountId, CachedRecordPart.INSIGHTS_AFFECTION, PARTICIPANTS_MANIFEST)
        ?: throw InsightsException(InsightsFailure.CURSOR_INVALID)
      val pages = participantPages(accountId)
      if (pages.lastOrNull()?.value?.nextCursor != cursor) throw InsightsException(InsightsFailure.CURSOR_INVALID)
      manifest to pages
    }
    requireActive(accountId)
    val page = remote.insightsParticipants(token, cursor)
    requireActive(accountId)
    validateRemote { validateParticipantPages(previous.second.map { it.value } + page) }
    return locked(accountId) {
      requireManifest(accountId, CachedRecordPart.INSIGHTS_AFFECTION, PARTICIPANTS_MANIFEST, previous.first)
      val id = "insights-participants-${previous.first.generation}-${previous.first.pageIds.size}"
      val saved = save(accountId, id, CachedRecordPart.INSIGHTS_AFFECTION, page) { it.validate() }
      saveManifest(accountId, CachedRecordPart.INSIGHTS_AFFECTION, PARTICIPANTS_MANIFEST,
        previous.first.copy(pageIds = previous.first.pageIds + id))
      (previous.second + saved).map { it.copyValue(restoreParticipants(accountId, it.value)) }
    }
  }

  suspend fun cachedRequesters(accountId: String): List<InsightsSaved<InsightsRequesterPage>> = locked(accountId) {
    requesterPages(accountId).map { saved -> saved.copyValue(restoreRequesters(accountId, saved.value)) }
  }

  suspend fun refreshRequesters(token: String, accountId: String): List<InsightsSaved<InsightsRequesterPage>> {
    locked(accountId) { Unit }
    val page = remote.insightsRequesters(token)
    requireActive(accountId)
    return locked(accountId) {
      val generation = UUID.randomUUID().toString()
      val id = "insights-requesters-$generation-0"
      val saved = save(accountId, id, CachedRecordPart.INSIGHTS_REQUESTERS, page) { it.validate() }
      saveManifest(accountId, CachedRecordPart.INSIGHTS_REQUESTERS, REQUESTERS_MANIFEST,
        InsightManifest(1, generation, page.generatedAt, listOf(id)))
      prunePages(accountId, CachedRecordPart.INSIGHTS_REQUESTERS, REQUESTERS_MANIFEST, setOf(id))
      pruneAvatars(accountId)
      listOf(saved.copyValue(restoreRequesters(accountId, page)))
    }
  }

  suspend fun appendRequesters(token: String, accountId: String, cursor: String): List<InsightsSaved<InsightsRequesterPage>> {
    val previous = locked(accountId) {
      val manifest = manifest(accountId, CachedRecordPart.INSIGHTS_REQUESTERS, REQUESTERS_MANIFEST)
        ?: throw InsightsException(InsightsFailure.CURSOR_INVALID)
      val pages = requesterPages(accountId)
      if (pages.lastOrNull()?.value?.nextCursor != cursor) throw InsightsException(InsightsFailure.CURSOR_INVALID)
      manifest to pages
    }
    requireActive(accountId)
    val page = remote.insightsRequesters(token, cursor)
    requireActive(accountId)
    validateRemote { validateRequesterPages(previous.second.map { it.value } + page) }
    return locked(accountId) {
      requireManifest(accountId, CachedRecordPart.INSIGHTS_REQUESTERS, REQUESTERS_MANIFEST, previous.first)
      val id = "insights-requesters-${previous.first.generation}-${previous.first.pageIds.size}"
      val saved = save(accountId, id, CachedRecordPart.INSIGHTS_REQUESTERS, page) { it.validate() }
      saveManifest(accountId, CachedRecordPart.INSIGHTS_REQUESTERS, REQUESTERS_MANIFEST,
        previous.first.copy(pageIds = previous.first.pageIds + id))
      (previous.second + saved).map { it.copyValue(restoreRequesters(accountId, it.value)) }
    }
  }

  private suspend fun participantPages(accountId: String): List<InsightsSaved<InsightsAffectionPage>> {
    val manifest = manifest(accountId, CachedRecordPart.INSIGHTS_AFFECTION, PARTICIPANTS_MANIFEST) ?: return emptyList()
    val pages = manifest.pageIds.map { id ->
      load<InsightsAffectionPage>(accountId, id, CachedRecordPart.INSIGHTS_AFFECTION) { it.validate() }
        ?: throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE)
    }
    validateCached { validateParticipantPages(pages.map { it.value }); check(pages.first().value.generatedAt == manifest.generatedAt) }
    return pages
  }

  private suspend fun requesterPages(accountId: String): List<InsightsSaved<InsightsRequesterPage>> {
    val manifest = manifest(accountId, CachedRecordPart.INSIGHTS_REQUESTERS, REQUESTERS_MANIFEST) ?: return emptyList()
    val pages = manifest.pageIds.map { id ->
      load<InsightsRequesterPage>(accountId, id, CachedRecordPart.INSIGHTS_REQUESTERS) { it.validate() }
        ?: throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE)
    }
    validateCached { validateRequesterPages(pages.map { it.value }); check(pages.first().value.generatedAt == manifest.generatedAt) }
    return pages
  }

  private suspend fun manifest(accountId: String, part: CachedRecordPart, id: String): InsightManifest? =
    load<InsightManifest>(accountId, id, part) { it.validate() }?.value

  private suspend fun saveManifest(accountId: String, part: CachedRecordPart, id: String, value: InsightManifest) {
    save(accountId, id, part, value) { it.validate() }
  }

  private suspend fun requireManifest(accountId: String, part: CachedRecordPart, id: String, expected: InsightManifest) {
    if (manifest(accountId, part, id) != expected) throw InsightsException(InsightsFailure.CURSOR_INVALID)
  }

  private suspend fun prunePages(accountId: String, part: CachedRecordPart, manifestId: String, keep: Set<String>) {
    requireActive(accountId)
    // Reclaim only this view's part. Record sync and its avatar pruning cannot touch these rows.
    // A failed cleanup leaves orphan ciphertext; the already committed snapshot remains usable.
    try { cache.deletePartRecords(accountId, part, cache.recordIds(accountId, part).toSet() - keep - manifestId) }
    catch (_: RecordCacheException) { /* The next successful refresh retries this bounded cleanup. */ }
    requireActive(accountId)
  }

  private suspend fun pruneAvatars(accountId: String) {
    try {
      val rankings = load<InsightsRankings>(accountId, RANKINGS_ID, CachedRecordPart.INSIGHTS_RANKINGS) { it.validate() }?.value
      val avatars = (rankings?.let { it.wins + it.requests }.orEmpty().map { it.avatar } +
        participantPages(accountId).flatMap { it.value.rankings }.flatMap { it.entries }.map { it.avatar } +
        requesterPages(accountId).flatMap { it.value.requesters }.map { it.avatar })
      val used = avatars.mapNotNull { it.url }.map(::avatarId).toSet()
      cache.deletePartRecords(accountId, CachedRecordPart.INSIGHTS_AVATAR,
        cache.recordIds(accountId, CachedRecordPart.INSIGHTS_AVATAR).toSet() - used)
    } catch (_: RecordCacheException) { /* Old ciphertext can be reclaimed by a later refresh. */ }
    catch (error: InsightsException) { if (error.failure == InsightsFailure.AUTH_REQUIRED) throw error }
    requireActive(accountId)
  }

  /** Optional enrichment runs after the caller has displayed the committed numbers. */
  suspend fun warmRankingsAvatars(accountId: String) {
    val avatars = locked(accountId) {
      load<InsightsRankings>(accountId, RANKINGS_ID, CachedRecordPart.INSIGHTS_RANKINGS) { it.validate() }
        ?.value?.let { it.wins + it.requests }.orEmpty().map { it.avatar }
    }
    cacheAvatars(accountId, avatars)
  }

  suspend fun warmParticipantsAvatars(accountId: String) {
    val avatars = locked(accountId) { participantPages(accountId).flatMap { it.value.rankings }.flatMap { it.entries }.map { it.avatar } }
    cacheAvatars(accountId, avatars)
  }

  suspend fun warmRequestersAvatars(accountId: String) {
    val avatars = locked(accountId) { requesterPages(accountId).flatMap { it.value.requesters }.map { it.avatar } }
    cacheAvatars(accountId, avatars)
  }

  private suspend fun cacheAvatars(accountId: String, avatars: List<RecordAvatar>) {
    val concurrency = Semaphore(4)
    withTimeoutOrNull(5_000) {
      coroutineScope {
        avatars.mapNotNull { it.url }.distinctBy(::avatarSource).map { url ->
          async { concurrency.withPermit { cacheAvatar(accountId, url) } }
        }.awaitAll()
      }
    }
    requireActive(accountId)
    locked(accountId) { pruneAvatars(accountId) }
  }

  private suspend fun cacheAvatar(accountId: String, url: String) {
    requireActive(accountId)
    val id = avatarId(url)
    val exists = locked(accountId) {
      try { load<InsightAvatar>(accountId, id, CachedRecordPart.INSIGHTS_AVATAR) { it.validate(id) } != null }
      catch (error: InsightsException) { if (error.failure == InsightsFailure.AUTH_REQUIRED) throw error; false }
    }
    if (exists) return
    var bytes: ByteArray? = null
    try {
      bytes = remote.avatar(url, allowParticipants = true)?.let { thumbnailAvatar(it) }
      requireActive(accountId)
      locked(accountId) {
        save(accountId, id, CachedRecordPart.INSIGHTS_AVATAR,
          InsightAvatar(id, bytes?.let { Base64.getEncoder().encodeToString(it) })) { it.validate(id) }
      }
    } catch (error: CancellationException) { throw error }
    catch (error: InsightsException) { if (error.failure == InsightsFailure.AUTH_REQUIRED) throw error }
    catch (_: RecordReadException) { /* Missing/transient images retain the standard fallback. */ }
    finally { bytes?.fill(0) }
  }

  private suspend fun restoreAvatar(accountId: String, avatar: RecordAvatar): RecordAvatar {
    requireActive(accountId)
    val value = avatar.url?.let { url ->
      val id = avatarId(url)
      try { load<InsightAvatar>(accountId, id, CachedRecordPart.INSIGHTS_AVATAR) { it.validate(id) }?.value }
      catch (error: InsightsException) { if (error.failure == InsightsFailure.AUTH_REQUIRED) throw error; null }
    }
    val bytes = value?.data?.let { Base64.getDecoder().decode(it) }
    try { requireActive(accountId) } catch (error: Exception) { bytes?.fill(0); throw error }
    return RecordAvatar(null, avatar.fallbackVariant, bytes)
  }

  private suspend fun restoreRankings(accountId: String, value: InsightsRankings): InsightsRankings = value.copy(
    wins = value.wins.map { it.copy(avatar = restoreAvatar(accountId, it.avatar)) },
    requests = value.requests.map { it.copy(avatar = restoreAvatar(accountId, it.avatar)) })
  private suspend fun restoreParticipants(accountId: String, value: InsightsAffectionPage): InsightsAffectionPage = value.copy(
    rankings = value.rankings.map { ranking -> ranking.copy(entries = ranking.entries.map { it.copy(avatar = restoreAvatar(accountId, it.avatar)) }) })
  private suspend fun restoreRequesters(accountId: String, value: InsightsRequesterPage): InsightsRequesterPage = value.copy(
    requesters = value.requesters.map { it.copy(avatar = restoreAvatar(accountId, it.avatar)) })

  private suspend inline fun <reified T> save(accountId: String, id: String, part: CachedRecordPart,
    value: T, validate: (T) -> Unit): InsightsSaved<T> {
    requireActive(accountId)
    val saved = InsightsSaved(value, Instant.now())
    val bytes = try {
      validate(value)
      json.encodeToString(InsightEnvelope(1, saved.fetchedAt.toString(), json.encodeToString(value))).toByteArray(Charsets.UTF_8)
    } catch (error: CancellationException) { throw error }
    catch (_: Exception) { throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE) }
    try {
      requireActive(accountId)
      cache.save(accountId, id, part, bytes)
      requireActive(accountId)
    } catch (_: RecordCacheException) { throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE) }
    finally { bytes.fill(0) }
    return saved
  }

  private suspend inline fun <reified T> load(accountId: String, id: String, part: CachedRecordPart,
    validate: (T) -> Unit): InsightsSaved<T>? {
    requireActive(accountId)
    val bytes = try { cache.load(accountId, id, part) }
    catch (_: RecordCacheException) { throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE) } ?: return null
    try {
      val envelope = json.decodeFromString<InsightEnvelope>(bytes.decodeToString(throwOnInvalidSequence = true))
      check(envelope.schemaVersion == 1)
      val value = json.decodeFromString<T>(envelope.payload).also(validate)
      requireActive(accountId)
      return InsightsSaved(value, Instant.parse(envelope.fetchedAt))
    } catch (error: CancellationException) { throw error }
    catch (error: InsightsException) { throw error }
    catch (_: Exception) { throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE) }
    finally { bytes.fill(0) }
  }

  private suspend fun <T> locked(accountId: String, operation: suspend () -> T): T = RecordCacheAccount.lock.withLock {
    requireActive(accountId)
    try { account.requireOwner(accountId) }
    catch (_: RecordCacheException) { throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE) }
    requireActive(accountId)
    operation().also { requireActive(accountId) }
  }

  private fun requireActive(accountId: String) {
    if (closed) throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE)
    if (!isActiveAccount(accountId)) throw InsightsException(InsightsFailure.AUTH_REQUIRED)
  }
  private fun validateRemote(block: () -> Unit) {
    try { block() } catch (_: Exception) { throw InsightsException(InsightsFailure.INVALID_RESPONSE) }
  }
  private fun validateCached(block: () -> Unit) {
    try { block() } catch (_: Exception) { throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE) }
  }
  private fun <T> InsightsSaved<T>.copyValue(value: T) = InsightsSaved(value, fetchedAt)

  override fun close() {
    if (closed) return
    closed = true
    try { remote.close() } finally { database.close() }
  }

  companion object {
    private const val RANKINGS_ID = "insights-rankings-v1"
    private const val PARTICIPANTS_MANIFEST = "insights-participants-manifest-v1"
    private const val REQUESTERS_MANIFEST = "insights-requesters-manifest-v1"
    private fun costsId(period: InsightsPeriod) = "insights-costs-${period.wireValue}-v1"
    private fun avatarId(url: String) = "insights-avatar-${avatarSource(url)}"

    fun open(context: Context, isActiveAccount: (String) -> Boolean): InsightsRepository {
      val database = EncryptedRecordsDatabase.open(context)
      try {
        val keys = KeystorePrivateKeyStore(context)
        return InsightsRepository(RecordsReadClient(), EncryptedRecordStore(database, RecordDataKeyProtector(keys)),
          database, RecordCacheAccount(context, keys, database), isActiveAccount)
      } catch (error: Exception) { database.close(); throw error }
    }
  }
}

@Serializable
private data class InsightEnvelope(val schemaVersion: Int, val fetchedAt: String, val payload: String)
@Serializable
private data class InsightManifest(val schemaVersion: Int, val generation: String, val generatedAt: String, val pageIds: List<String>) {
  fun validate() {
    check(schemaVersion == 1 && UUID.fromString(generation).toString() == generation)
    insightTimestamp(generatedAt)
    check(pageIds.isNotEmpty() && pageIds.distinct().size == pageIds.size)
    check(pageIds.all { it.matches(Regex("insights-(participants|requesters)-$generation-[0-9]+")) })
  }
}
@Serializable
private data class InsightAvatar(val source: String, val data: String?) {
  fun validate(expected: String) {
    check(source == expected)
    data?.let { encoded ->
      check(encoded.length <= 174_764)
      val decoded = Base64.getDecoder().decode(encoded)
      try { check(decoded.size in 1..131_072) } finally { decoded.fill(0) }
    }
  }
}
