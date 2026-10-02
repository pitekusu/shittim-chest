package dev.pitekusu.shittim.records

import android.content.Context
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordRow
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheException
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import java.io.Closeable
import java.time.Instant
import java.util.Base64
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.serialization.KSerializer
import kotlinx.serialization.Serializable
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.Json
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Keeps the existing authenticated API boundary and writes only encrypted, validated records. */
internal class RecordsRepository(
  private val remote: RecordsReadClient,
  private val cache: EncryptedRecordStore,
  private val database: EncryptedRecordsDatabase,
  private val account: RecordCacheAccount,
  private val isActiveAccount: (String) -> Boolean,
  private val activateOwner: Boolean = true,
) : Closeable {
  private val json = Json { encodeDefaults = true }
  private val memoLock = Any()
  @Volatile private var closed = false
  private var memoAccountId: String? = null
  private val listMemo = mutableMapOf<String, MemoizedListEntry>()
  private val avatarMemo = mutableMapOf<String, MemoizedAvatar>()

  suspend fun recentRecords(token: String, accountId: String, cursor: String?): RecordListPage {
    requireActive(accountId)
    val page = remote.recentRecords(token, cursor)
    accountLock.withLock {
      prepare(accountId)
      page.items.forEach { entry ->
        save(accountId, entry.recordId, CachedRecordPart.LIST) {
          json.encodeToString(CachedListEntry(1, entry))
        }
      }
      requireActive(accountId)
    }
    return page
  }

  suspend fun record(token: String, accountId: String, recordId: String,
    avatarRevision: String? = null): RecordReadResult {
    requireActive(accountId)
    val result = try { remote.firstRecord(token, "/records/$recordId") }
    catch (error: RecordReadException) {
      if (error.failure == RecordReadFailure.NOT_FOUND) removeRecords(accountId, setOf(recordId))
      throw error
    }
    if (result is RecordReadResult.Found) {
      accountLock.withLock {
        prepare(accountId)
        save(accountId, recordId, CachedRecordPart.DETAIL) {
          json.encodeToString(CachedDetail(1, result.preview))
        }
        result.listEntry?.let { entry ->
          val savedEntry = RecordListEntry(entry.recordId, entry.questionPreview, entry.requesterName,
            RecordAvatar(entry.requesterAvatar.url, entry.requesterAvatar.fallbackVariant, revision = avatarRevision),
            entry.completedAt, entry.winnerName, entry.winnerSlot)
          save(accountId, recordId, CachedRecordPart.LIST) { json.encodeToString(CachedListEntry(1, savedEntry)) }
        }
        requireActive(accountId)
      }
    }
    return result
  }

  suspend fun cachedRecordIds(accountId: String): Set<String> = accountLock.withLock {
    prepareRead(accountId)
    val ids = try {
      (cache.recordIds(accountId, CachedRecordPart.LIST) +
        cache.recordIds(accountId, CachedRecordPart.DETAIL)).toSet()
    } catch (_: RecordCacheException) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
    requireActive(accountId)
    ids
  }

  /** Only a committed revision plus both cached parts may skip a remote detail read. */
  suspend fun cachedCompleteRecordIds(accountId: String): Set<String> = accountLock.withLock {
    prepareRead(accountId)
    val ids = try {
      cache.recordIds(accountId, CachedRecordPart.LIST).toSet()
        .intersect(cache.recordIds(accountId, CachedRecordPart.DETAIL).toSet())
    } catch (_: RecordCacheException) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
    requireActive(accountId)
    ids
  }

  suspend fun cachedRecords(accountId: String): List<RecordListEntry> {
    try {
      val (listRows, avatarRows) = accountLock.withLock {
        prepareRead(accountId)
        val lists = cache.rows(accountId, CachedRecordPart.LIST)
        val avatars = cache.rows(accountId, CachedRecordPart.AVATAR)
        prepareRead(accountId)
        lists to avatars
      }
      // A cold read yields the account lock between rows so fresh worker writes can proceed.
      val wantedAvatars = listRows.mapNotNull { row ->
        accountLock.withLock {
          prepareRead(accountId)
          avatarCacheKey(memoizedListEntry(accountId, row)).also { requireActive(accountId) }
        }
      }.toSet()
      avatarRows.filter { it.recordId in wantedAvatars }.forEach { row ->
        accountLock.withLock {
          prepareRead(accountId)
          memoizeAvatar(accountId, row)
          requireActive(accountId)
        }
      }
      return accountLock.withLock {
        prepareRead(accountId)
        // Reconcile only the final delta under the lock. This also rejects a stale snapshot
        // when logout cleared the DB and the same account signed in again between rows.
        val entries = memoizedListEntries(accountId, cache.rows(accountId, CachedRecordPart.LIST))
        val avatars = cache.rows(accountId, CachedRecordPart.AVATAR).associateBy { it.recordId }
        synchronized(memoLock) {
          (avatarMemo.keys - avatars.keys).forEach { avatarMemo.remove(it)?.bytes?.fill(0) }
        }
        val result = entries.sortedByDescending { it.completedAt }.map { entry ->
          requireActive(accountId)
          val row = avatarCacheKey(entry)?.let(avatars::get)
          val bytes = row?.let {
            memoizeAvatar(accountId, it)
            synchronized(memoLock) {
              requireActive(accountId)
              // The memo owns its bytes; clearing it must not mutate bytes handed to the UI.
              avatarMemo[it.recordId]?.bytes?.copyOf()
            }
          }
          // Presigned URLs expire and offline browsing must not trigger image network requests.
          RecordListEntry(entry.recordId, entry.questionPreview, entry.requesterName,
            RecordAvatar(null, entry.requesterAvatar.fallbackVariant, bytes),
            entry.completedAt, entry.winnerName, entry.winnerSlot)
        }
        prepareRead(accountId)
        result
      }
    } catch (error: RecordReadException) {
      clearMemo()
      throw error
    } catch (_: RecordCacheException) {
      clearMemo()
      throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
    }
  }

  private suspend fun memoizedListEntries(accountId: String, rows: List<EncryptedRecordRow>): List<RecordListEntry> {
    synchronized(memoLock) { listMemo.keys.retainAll(rows.map { it.recordId }.toSet()) }
    return rows.map { memoizedListEntry(accountId, it) }
  }

  private suspend fun memoizedListEntry(accountId: String, row: EncryptedRecordRow): RecordListEntry {
    requireActive(accountId)
    val previous = synchronized(memoLock) { listMemo[row.recordId] }
    return if (previous != null && previous.row.sameCiphertext(row)) previous.entry else {
      val entry = decodeListEntry(row.recordId, cache.decryptRow(accountId, row, CachedRecordPart.LIST))
      synchronized(memoLock) {
        requireActive(accountId)
        listMemo[row.recordId] = MemoizedListEntry(row, entry)
      }
      entry
    }
  }

  private suspend fun memoizeAvatar(accountId: String, row: EncryptedRecordRow) {
    synchronized(memoLock) {
      requireActive(accountId)
      if (avatarMemo[row.recordId]?.row?.sameCiphertext(row) == true) return
    }
    val decoded = decode(cache.decryptRow(accountId, row, CachedRecordPart.AVATAR)) { serialized ->
      val avatar = json.decodeFromString<CachedAvatar>(serialized)
      check(avatar.source == row.recordId)
      avatar.data?.let { Base64.getDecoder().decode(it) }
    }
    synchronized(memoLock) {
      try {
        requireActive(accountId)
        avatarMemo.put(row.recordId, MemoizedAvatar(row, decoded))?.bytes?.fill(0)
      } catch (error: RecordReadException) {
        decoded?.fill(0)
        throw error
      }
    }
  }

  suspend fun syncIndex(token: String, accountId: String, cursor: String?): RecordSyncIndex {
    requireActive(accountId)
    return remote.syncIndex(token, cursor).also { requireActive(accountId) }
  }

  suspend fun queryCachedRecords(accountId: String, entries: List<RecordListEntry>,
    query: RecordListQuery): List<RecordListEntry> = withContext(Dispatchers.Default) {
    requireActive(accountId)
    val matches = entries.filter { entry ->
      requireActive(accountId)
      query.acceptsWinner(entry) && query.acceptsRequester(entry) && (!query.searchesText || query.matches(entry) ||
        query.matches(entry, cachedRecord(accountId, entry.recordId)))
    }
    requireActive(accountId)
    query.sorted(matches)
  }

  suspend fun cachedRevision(accountId: String, recordId: String): String? = accountLock.withLock {
    prepareRead(accountId)
    val revision = load(accountId, recordId, CachedRecordPart.REVISION)?.let { bytes ->
      decode(bytes) { json.decodeFromString<String>(it).also { value -> check(dev.pitekusu.shittim.records.auth.mobileOpaqueValue.matches(value)) } }
    }
    requireActive(accountId)
    revision
  }

  suspend fun saveRevision(accountId: String, reference: RecordSyncReference): Unit = accountLock.withLock {
    prepareRead(accountId)
    save(accountId, reference.recordId, CachedRecordPart.REVISION) { json.encodeToString(reference.revision) }
    requireActive(accountId)
  }

  suspend fun saveAvatar(accountId: String, entry: RecordListEntry) {
    val url = entry.requesterAvatar.url ?: return
    val source = checkNotNull(avatarCacheKey(entry))
    val existing = accountLock.withLock {
      prepareRead(accountId)
      load(accountId, source, CachedRecordPart.AVATAR)?.let { bytes ->
        decode(bytes) { json.decodeFromString<CachedAvatar>(it) }
      }
    }
    if (existing?.source == source) return
    requireActive(accountId)
    val bytes = remote.avatar(url)?.let { thumbnailAvatar(it) }
    try {
      accountLock.withLock {
        prepareRead(accountId)
        save(accountId, source, CachedRecordPart.AVATAR) {
          json.encodeToString(CachedAvatar(source, bytes?.let { Base64.getEncoder().encodeToString(it) }))
        }
        requireActive(accountId)
      }
    } finally { bytes?.fill(0) }
  }

  suspend fun pruneAvatars(accountId: String): Unit = accountLock.withLock {
    prepareRead(accountId)
    try {
      val used = memoizedListEntries(accountId, cache.rows(accountId, CachedRecordPart.LIST))
        .mapNotNull(::avatarCacheKey).toSet()
      val removed = cache.recordIds(accountId, CachedRecordPart.AVATAR).toSet() - used
      cache.deleteRecords(accountId, removed)
      synchronized(memoLock) { removed.forEach { avatarMemo.remove(it)?.bytes?.fill(0) } }
    } catch (error: RecordReadException) {
      clearMemo()
      throw error
    } catch (_: RecordCacheException) {
      clearMemo()
      throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
    }
    prepareRead(accountId)
  }

  private fun avatarCacheKey(entry: RecordListEntry): String? = entry.requesterAvatar.url?.let {
    "avatar-${avatarSource(it)}-${entry.requesterAvatar.revision ?: "legacy"}"
  }

  suspend fun removeRecords(accountId: String, recordIds: Set<String>): Unit = accountLock.withLock {
    prepareRead(accountId)
    try { cache.deleteRecords(accountId, recordIds) }
    catch (_: RecordCacheException) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
    requireActive(accountId)
  }

  suspend fun removeIfDeleted(token: String, accountId: String, recordId: String) {
    requireActive(accountId)
    try {
      // An eventually consistent index omission is not evidence of deletion.
      // Confirm against the existing strongly consistent detail API, without
      // overwriting a surviving record or its cached avatar metadata.
      if (remote.firstRecord(token, "/records/$recordId") !is RecordReadResult.Found) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
    } catch (error: RecordReadException) {
      if (error.failure != RecordReadFailure.NOT_FOUND) throw error
      removeRecords(accountId, setOf(recordId))
    }
    requireActive(accountId)
  }

  suspend fun syncCheckpoint(accountId: String): RecordSyncCheckpoint? = accountLock.withLock {
    prepare(accountId)
    val checkpoint = load(accountId, SYNC_RECORD_ID, CachedRecordPart.SYNC_PROGRESS)?.let { bytes ->
      decode(bytes) { json.decodeFromString<RecordSyncCheckpoint>(it).also { state -> state.validate() } }
    }
    requireActive(accountId)
    checkpoint
  }

  suspend fun saveSyncCheckpoint(accountId: String, checkpoint: RecordSyncCheckpoint): Unit = accountLock.withLock {
    prepare(accountId)
    save(accountId, SYNC_RECORD_ID, CachedRecordPart.SYNC_PROGRESS) {
      checkpoint.validate()
      json.encodeToString(checkpoint)
    }
    requireActive(accountId)
  }

  // Offline readers use the session's persisted permit; they cannot activate another owner.
  suspend fun cachedListEntry(accountId: String, recordId: String): RecordListEntry? = accountLock.withLock {
    prepareRead(accountId)
    val result = load(accountId, recordId, CachedRecordPart.LIST)?.let { decodeListEntry(recordId, it) }
    requireActive(accountId)
    result
  }

  suspend fun cachedRecord(accountId: String, recordId: String): RecordPreview? = accountLock.withLock {
    prepareRead(accountId)
    val result = load(accountId, recordId, CachedRecordPart.DETAIL)?.let { serialized ->
      decode(serialized) {
        json.decodeFromString<CachedDetail>(it).also { cached ->
          check(cached.schemaVersion == 1)
        }.preview
      }
    }
    requireActive(accountId)
    result
  }

  private suspend fun prepareRead(accountId: String) {
    requireActive(accountId)
    try { account.requireOwner(accountId) }
    catch (_: RecordCacheException) {
      clearMemo()
      throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
    }
    requireActive(accountId)
    bindMemo(accountId)
  }

  private fun requireActive(accountId: String) {
    if (closed) {
      clearMemo()
      throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
    }
    if (!isActiveAccount(accountId)) {
      clearMemo()
      throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
    }
  }

  private suspend fun prepare(accountId: String) {
    if (!activateOwner) { prepareRead(accountId); return }
    requireActive(accountId)
    try { account.activate(accountId) }
    catch (_: RecordCacheException) {
      clearMemo()
      throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
    }
    requireActive(accountId)
    bindMemo(accountId)
  }

  private fun bindMemo(accountId: String): Unit = synchronized(memoLock) {
    if (memoAccountId != accountId) {
      clearMemo()
      memoAccountId = accountId
    }
  }

  private fun clearMemo(): Unit = synchronized(memoLock) {
    listMemo.clear()
    avatarMemo.values.forEach { it.bytes?.fill(0) }
    avatarMemo.clear()
    memoAccountId = null
  }

  private suspend fun save(accountId: String, recordId: String, part: CachedRecordPart, encode: () -> String) {
    val bytes = try {
      encode().toByteArray(Charsets.UTF_8)
    } catch (_: Exception) {
      throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
    }
    try {
      cache.save(accountId, recordId, part, bytes)
    } catch (error: RecordCacheException) {
      // Never return a successful online result after its local save failed.
      throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
    } finally {
      bytes.fill(0)
    }
  }

  private suspend fun load(accountId: String, recordId: String, part: CachedRecordPart): ByteArray? = try {
    cache.load(accountId, recordId, part)
  } catch (error: RecordCacheException) {
    throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
  }

  private fun <T> decode(bytes: ByteArray, parse: (String) -> T): T = try {
    parse(bytes.decodeToString(throwOnInvalidSequence = true))
  } catch (error: CancellationException) {
    throw error
  } catch (_: Exception) {
    throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
  } finally {
    bytes.fill(0)
  }

  private fun decodeListEntry(recordId: String, bytes: ByteArray): RecordListEntry = decode(bytes) {
    json.decodeFromString<CachedListEntry>(it).also { cached ->
      check(cached.schemaVersion == 1 && cached.entry.recordId == recordId)
    }.entry
  }

  override fun close() {
    closed = true
    clearMemo()
    try { remote.close() } finally { database.close() }
  }

  private class MemoizedListEntry(val row: EncryptedRecordRow, val entry: RecordListEntry)
  private class MemoizedAvatar(val row: EncryptedRecordRow, val bytes: ByteArray?)

  private fun EncryptedRecordRow.sameCiphertext(other: EncryptedRecordRow): Boolean =
    accountKey == other.accountKey && recordId == other.recordId && part == other.part &&
      wrappedKey.contentEquals(other.wrappedKey) && encryptedPayload.contentEquals(other.encryptedPayload)

  companion object {
    private val accountLock = RecordCacheAccount.lock
    private const val SYNC_RECORD_ID = "record-sync-v1" // Separate authenticated part, not an API record.

    fun open(context: Context, isActiveAccount: (String) -> Boolean, activateOwner: Boolean = true): RecordsRepository {
      val database = EncryptedRecordsDatabase.open(context)
      try {
        val privateKeys = KeystorePrivateKeyStore(context)
        val cache = EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
        return RecordsRepository(RecordsReadClient(), cache, database,
          RecordCacheAccount(context, privateKeys, database), isActiveAccount, activateOwner)
      } catch (error: Exception) {
        database.close()
        throw error
      }
    }
  }
}

@Serializable
private class CachedListEntry(val schemaVersion: Int, val entry: RecordListEntry)

@Serializable
private class CachedDetail(val schemaVersion: Int, val preview: RecordPreview)

@Serializable
private class CachedAvatar(val source: String, val data: String?)

internal object CachedRecordInstantSerializer : KSerializer<Instant> {
  override val descriptor = PrimitiveSerialDescriptor("CachedRecordInstant", PrimitiveKind.STRING)
  override fun serialize(encoder: Encoder, value: Instant) = encoder.encodeString(value.toString())
  override fun deserialize(decoder: Decoder): Instant = Instant.parse(decoder.decodeString())
}
