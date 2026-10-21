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
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/** All durable content uses the account's existing encrypted store. Presigned URLs live only in memory. */
internal class MomotalkRepository(private val remote: RecordsReadClient, private val cache: EncryptedRecordStore,
  private val database: EncryptedRecordsDatabase, private val account: RecordCacheAccount,
  private val isActiveAccount: (String) -> Boolean) : Closeable {
  private val json = Json { encodeDefaults = true }
  private val roomSources = ConcurrentHashMap<String, MomotalkRoomDetail>()
  private val listSources = ConcurrentHashMap<String, List<MomotalkRoomSummary>>()
  @Volatile private var closed = false

  suspend fun cachedWeeks(owner: String): List<MomotalkSaved<MomotalkWeeksPage>> = locked(owner) {
    load<List<MomotalkWeeksPage>>(owner, WEEKS, CachedRecordPart.MOMOTALK_WEEKS, ::validateWeeks)
      ?.let { (envelope, pages) -> pages.map { MomotalkSaved(it, Instant.parse(envelope.fetchedAt)) } }.orEmpty()
  }
  suspend fun refreshWeeks(token: String, owner: String): List<MomotalkSaved<MomotalkWeeksPage>> {
    locked(owner) { Unit }
    val page = remote.momotalkWeeks(token)
    return locked(owner) {
      val saved = save(owner, WEEKS, CachedRecordPart.MOMOTALK_WEEKS, listOf(page), ::validateWeeks)
      listOf(MomotalkSaved(page, saved))
    }
  }
  suspend fun appendWeeks(token: String, owner: String, cursor: String): List<MomotalkSaved<MomotalkWeeksPage>> {
    val previous = locked(owner) {
      load<List<MomotalkWeeksPage>>(owner, WEEKS, CachedRecordPart.MOMOTALK_WEEKS, ::validateWeeks)
        ?.also { checkCursor(it.second.last().nextCursor, cursor) }
        ?: throw MomotalkException(MomotalkFailure.CURSOR_INVALID)
    }
    val page = remote.momotalkWeeks(token, cursor)
    return locked(owner) {
      requireRevision(owner, WEEKS, CachedRecordPart.MOMOTALK_WEEKS, previous.first.revision)
      val pages = previous.second + page
      validateRemote { validateWeeks(pages) }
      val saved = save(owner, WEEKS, CachedRecordPart.MOMOTALK_WEEKS, pages, ::validateWeeks)
      pages.map { MomotalkSaved(it, saved) }
    }
  }

  suspend fun cachedRooms(owner: String, weekId: String): List<MomotalkSaved<MomotalkRoomsPage>> = locked(owner) {
    load<List<MomotalkRoomsPage>>(owner, roomsId(weekId), CachedRecordPart.MOMOTALK_ROOMS, ::validateRooms)
      ?.let { (envelope, pages) -> pages.map { MomotalkSaved(restoreRooms(owner, it), Instant.parse(envelope.fetchedAt)) } }.orEmpty()
  }
  suspend fun refreshRooms(token: String, owner: String, weekId: String): List<MomotalkSaved<MomotalkRoomsPage>> {
    locked(owner) { Unit }
    val page = remote.momotalkRooms(token, weekId)
    return locked(owner) {
      val saved = save(owner, roomsId(weekId), CachedRecordPart.MOMOTALK_ROOMS,
        listOf(page.withoutUrls()), ::validateRooms)
      listSources[sourceKey(owner, weekId)] = page.rooms
      listOf(MomotalkSaved(restoreRooms(owner, page), saved))
    }
  }
  suspend fun appendRooms(token: String, owner: String, weekId: String, cursor: String): List<MomotalkSaved<MomotalkRoomsPage>> {
    val previous = locked(owner) {
      load<List<MomotalkRoomsPage>>(owner, roomsId(weekId), CachedRecordPart.MOMOTALK_ROOMS, ::validateRooms)
        ?.also { checkCursor(it.second.last().nextCursor, cursor) }
        ?: throw MomotalkException(MomotalkFailure.CURSOR_INVALID)
    }
    val page = remote.momotalkRooms(token, weekId, cursor)
    return locked(owner) {
      requireRevision(owner, roomsId(weekId), CachedRecordPart.MOMOTALK_ROOMS, previous.first.revision)
      val pages = previous.second + page.withoutUrls()
      validateRemote { validateRooms(pages) }
      val saved = save(owner, roomsId(weekId), CachedRecordPart.MOMOTALK_ROOMS, pages, ::validateRooms)
      val key = sourceKey(owner, weekId)
      listSources[key] = listSources[key].orEmpty() + page.rooms
      pages.map { MomotalkSaved(restoreRooms(owner, it), saved) }
    }
  }

  suspend fun cachedRoom(owner: String, weekId: String, roomId: String): MomotalkSaved<MomotalkRoomDetail>? = locked(owner) {
    load<MomotalkRoomDetail>(owner, detailId(weekId, roomId), CachedRecordPart.MOMOTALK_DETAIL) { it.validate() }
      ?.let { (envelope, detail) -> MomotalkSaved(restoreRoom(owner, detail), Instant.parse(envelope.fetchedAt)) }
  }
  suspend fun refreshRoom(token: String, owner: String, weekId: String, roomId: String): MomotalkSaved<MomotalkRoomDetail> {
    locked(owner) { Unit }
    val detail = remote.momotalkRoom(token, weekId, roomId)
    return locked(owner) {
      val saved = save(owner, detailId(weekId, roomId), CachedRecordPart.MOMOTALK_DETAIL, detail.cached()) { it.validate() }
      roomSources[sourceKey(owner, weekId, roomId)] = detail
      MomotalkSaved(restoreRoom(owner, detail), saved)
    }
  }

  /** Optional media never delays the saved conversation or converts an image failure into a text failure. */
  suspend fun warmRoomImages(owner: String, weekId: String, roomId: String) {
    val detail = locked(owner) { roomSources[sourceKey(owner, weekId, roomId)] } ?: return
    enrich(owner, detail.images.filter { it.state == "ready" }.map { image -> suspend {
      val id = photoId(weekId, roomId, image.key)
      val url = image.thumbnailUrl
      if (url != null && !locked(owner) { hasMedia(owner, id) }) {
        val bytes = remote.momotalkImage(url, weekId, roomId)
        try {
          check(bytes.size <= MAX_THUMBNAIL_BYTES)
          locked(owner) { cache.save(owner, id, CachedRecordPart.MOMOTALK_MEDIA, bytes) }
        } finally { bytes.fill(0) }
      }
    } })
  }
  suspend fun warmRoomsAvatars(owner: String, weekId: String) {
    val rooms = locked(owner) { listSources[sourceKey(owner, weekId)].orEmpty() }
    enrich(owner, rooms.map { room -> suspend { warmAvatar(owner, requesterAvatarId(weekId, room.roomId), room.requester.avatar) } })
  }
  suspend fun warmRoomAvatars(owner: String, weekId: String, roomId: String) {
    val detail = locked(owner) { roomSources[sourceKey(owner, weekId, roomId)] } ?: return
    enrich(owner, listOf<suspend () -> Unit>(
      { warmAvatar(owner, requesterAvatarId(weekId, roomId), detail.room.requester.avatar) }) +
      detail.participants.map { person -> suspend { warmAvatar(owner, participantAvatarId(person.slot), person.avatar) } })
  }
  private suspend fun warmAvatar(owner: String, id: String, avatar: RecordAvatar) {
    val url = avatar.url ?: return
    if (locked(owner) { hasMedia(owner, id) }) return
    val bytes = remote.avatar(url, allowParticipants = true)?.let { thumbnailAvatar(it) } ?: return
    try { locked(owner) { cache.save(owner, id, CachedRecordPart.MOMOTALK_MEDIA, bytes) } }
    finally { bytes.fill(0) }
  }
  private suspend fun enrich(owner: String, jobs: List<suspend () -> Unit>) {
    val semaphore = Semaphore(3)
    withTimeoutOrNull(15_000) {
      coroutineScope { jobs.map { job -> async {
        semaphore.withPermit {
          try { requireActive(owner); job() }
          catch (error: CancellationException) { throw error }
          catch (error: MomotalkException) { if (error.failure == MomotalkFailure.AUTH_REQUIRED) throw error }
          catch (_: Exception) { /* The saved text and standard media fallback remain usable. */ }
        }
      } }.awaitAll() }
    }
    requireActive(owner)
  }

  private suspend fun restoreRooms(owner: String, page: MomotalkRoomsPage) = page.copy(rooms = page.rooms.map { room ->
    room.copy(requester = room.requester.copy(avatar = restoreAvatar(owner, requesterAvatarId(page.week.weekId, room.roomId), room.requester.avatar)))
  })
  private suspend fun restoreRoom(owner: String, detail: MomotalkRoomDetail): MomotalkRoomDetail {
    val weekId = detail.week.weekId
    val roomId = detail.room.roomId
    return detail.copy(room = detail.room.copy(requester = detail.room.requester.copy(
      avatar = restoreAvatar(owner, requesterAvatarId(weekId, roomId), detail.room.requester.avatar))),
      participants = detail.participants.map { it.copy(avatar = restoreAvatar(owner, participantAvatarId(it.slot), it.avatar)) },
      images = detail.images.map { it.cached().copy(bytes = if (it.state == "ready") media(owner, photoId(weekId, roomId, it.key)) else null) })
  }
  private suspend fun restoreAvatar(owner: String, id: String, avatar: RecordAvatar) =
    RecordAvatar(null, avatar.fallbackVariant, media(owner, id))
  private suspend fun media(owner: String, id: String): ByteArray? = try {
    cache.load(owner, id, CachedRecordPart.MOMOTALK_MEDIA)
  } catch (_: RecordCacheException) { null }
  private suspend fun hasMedia(owner: String, id: String): Boolean {
    val bytes = media(owner, id) ?: return false
    bytes.fill(0)
    return true
  }

  private suspend inline fun <reified T> save(owner: String, id: String, part: CachedRecordPart,
    value: T, validate: (T) -> Unit): Instant {
    requireActive(owner)
    val saved = Instant.now()
    val bytes = try {
      validate(value)
      json.encodeToString(MomotalkEnvelope(1, UUID.randomUUID().toString(), saved.toString(), json.encodeToString(value))).toByteArray(Charsets.UTF_8)
    } catch (_: Exception) { throw MomotalkException(MomotalkFailure.STORAGE_UNAVAILABLE) }
    try { cache.save(owner, id, part, bytes); requireActive(owner) }
    catch (_: RecordCacheException) { throw MomotalkException(MomotalkFailure.STORAGE_UNAVAILABLE) }
    finally { bytes.fill(0) }
    return saved
  }
  private suspend inline fun <reified T> load(owner: String, id: String, part: CachedRecordPart,
    validate: (T) -> Unit): Pair<MomotalkEnvelope, T>? {
    val bytes = try { cache.load(owner, id, part) }
    catch (_: RecordCacheException) { throw MomotalkException(MomotalkFailure.STORAGE_UNAVAILABLE) } ?: return null
    try {
      val envelope = json.decodeFromString<MomotalkEnvelope>(bytes.decodeToString(throwOnInvalidSequence = true))
      check(envelope.schemaVersion == 1); UUID.fromString(envelope.revision); Instant.parse(envelope.fetchedAt)
      val value = json.decodeFromString<T>(envelope.payload).also(validate)
      requireActive(owner)
      return envelope to value
    } catch (error: CancellationException) { throw error }
    catch (error: MomotalkException) { throw error }
    catch (_: Exception) { throw MomotalkException(MomotalkFailure.STORAGE_UNAVAILABLE) }
    finally { bytes.fill(0) }
  }
  private suspend fun requireRevision(owner: String, id: String, part: CachedRecordPart, expected: String) {
    val bytes = cache.load(owner, id, part) ?: throw MomotalkException(MomotalkFailure.CURSOR_INVALID)
    try {
      if (json.decodeFromString<MomotalkEnvelope>(bytes.decodeToString()).revision != expected)
        throw MomotalkException(MomotalkFailure.CURSOR_INVALID)
    } finally { bytes.fill(0) }
  }
  private suspend fun <T> locked(owner: String, block: suspend () -> T): T = RecordCacheAccount.lock.withLock {
    requireActive(owner)
    try { account.requireOwner(owner) }
    catch (_: RecordCacheException) { throw MomotalkException(MomotalkFailure.STORAGE_UNAVAILABLE) }
    requireActive(owner)
    block().also { requireActive(owner) }
  }
  private fun requireActive(owner: String) {
    if (closed) throw MomotalkException(MomotalkFailure.STORAGE_UNAVAILABLE)
    if (!isActiveAccount(owner)) {
      roomSources.clear(); listSources.clear()
      throw MomotalkException(MomotalkFailure.AUTH_REQUIRED)
    }
  }
  private fun checkCursor(actual: String?, expected: String) {
    if (actual != expected) throw MomotalkException(MomotalkFailure.CURSOR_INVALID)
  }
  private fun validateRemote(block: () -> Unit) {
    try { block() } catch (_: Exception) { throw MomotalkException(MomotalkFailure.INVALID_RESPONSE) }
  }
  override fun close() {
    if (closed) return
    closed = true; roomSources.clear(); listSources.clear()
    try { remote.close() } finally { database.close() }
  }
  companion object {
    private const val WEEKS = "momotalk-weeks-v1"
    // Room payloads have a 1 MiB bound; only the 320x480 thumbnail is stored here.
    private const val MAX_THUMBNAIL_BYTES = 1024 * 1024
    private fun roomsId(week: String) = "momotalk-rooms-$week"
    private fun detailId(week: String, room: String) = "momotalk-$week-$room"
    private fun photoId(week: String, room: String, image: String) = "momotalk-photo-$week-$room-$image"
    private fun requesterAvatarId(week: String, room: String) = "momotalk-avatar-$week-$room"
    private fun participantAvatarId(slot: String) = "momotalk-avatar-$slot"
    private fun sourceKey(owner: String, week: String, room: String = "") = "$owner/$week/$room"
    fun open(context: Context, isActiveAccount: (String) -> Boolean): MomotalkRepository {
      val database = EncryptedRecordsDatabase.open(context)
      try {
        val keys = KeystorePrivateKeyStore(context)
        return MomotalkRepository(RecordsReadClient(), EncryptedRecordStore(database, RecordDataKeyProtector(keys)),
          database, RecordCacheAccount(context, keys, database), isActiveAccount)
      } catch (error: Exception) { database.close(); throw error }
    }
  }
}
private fun MomotalkRoomsPage.withoutUrls() = copy(rooms = rooms.map { it.copy(requester = it.requester.copy(avatar = it.requester.avatar.withoutUrl())) })
private fun validateWeeks(pages: List<MomotalkWeeksPage>) {
  check(pages.isNotEmpty()); pages.forEach { it.validate() }
  val weeks = pages.flatMap { it.weeks }.map { it.weekId }
  check(weeks.distinct().size == weeks.size && weeks.zipWithNext().all { (a, b) -> a > b })
  check(pages.dropLast(1).all { it.nextCursor != null })
  check(pages.mapNotNull { it.nextCursor }.distinct().size == pages.mapNotNull { it.nextCursor }.size)
}
private fun validateRooms(pages: List<MomotalkRoomsPage>) {
  check(pages.isNotEmpty()); pages.forEach { it.validate(); check(it.week == pages.first().week) }
  val rooms = pages.flatMap { it.rooms }.map { it.roomId }
  check(rooms.distinct().size == rooms.size && rooms.zipWithNext().all { (a, b) -> a < b })
  check(pages.dropLast(1).all { it.nextCursor != null })
  check(pages.mapNotNull { it.nextCursor }.distinct().size == pages.mapNotNull { it.nextCursor }.size)
}
@Serializable
private data class MomotalkEnvelope(val schemaVersion: Int, val revision: String, val fetchedAt: String, val payload: String)
