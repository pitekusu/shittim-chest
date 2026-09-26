package dev.pitekusu.shittim.records

import android.content.Context
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
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
            entry.completedAt, entry.winnerName)
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

  suspend fun cachedRecords(accountId: String): List<RecordListEntry> {
    val ids = accountLock.withLock {
      prepareRead(accountId)
      try { cache.recordIds(accountId, CachedRecordPart.LIST) }
      catch (_: RecordCacheException) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
    }
    val icons = mutableMapOf<String, ByteArray?>()
    return ids.mapNotNull { cachedListEntry(accountId, it) }.sortedByDescending { it.completedAt }
      .map { entry ->
        // Presigned URLs expire and offline browsing must not trigger image network requests.
        RecordListEntry(entry.recordId, entry.questionPreview, entry.requesterName,
          RecordAvatar(null, entry.requesterAvatar.fallbackVariant,
            avatarCacheKey(entry)?.let { key -> icons.getOrPut(key) { cachedAvatar(accountId, entry) } }),
          entry.completedAt, entry.winnerName)
      }.also { requireActive(accountId) }
  }

  suspend fun syncIndex(token: String, accountId: String, cursor: String?): RecordSyncIndex {
    requireActive(accountId)
    return remote.syncIndex(token, cursor).also { requireActive(accountId) }
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

  private suspend fun cachedAvatar(accountId: String, entry: RecordListEntry): ByteArray? = accountLock.withLock {
    prepareRead(accountId)
    val source = avatarCacheKey(entry) ?: return@withLock null
    val avatar = load(accountId, source, CachedRecordPart.AVATAR)?.let { bytes ->
      decode(bytes) { json.decodeFromString<CachedAvatar>(it) }
    }
    val result = if (avatar?.source == source) avatar.data?.let { Base64.getDecoder().decode(it) } else null
    requireActive(accountId)
    result
  }

  suspend fun pruneAvatars(accountId: String) {
    val used = cachedRecordsForAvatarKeys(accountId)
    accountLock.withLock {
      prepareRead(accountId)
      try {
        cache.deleteRecords(accountId, cache.recordIds(accountId, CachedRecordPart.AVATAR).toSet() - used)
      } catch (_: RecordCacheException) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
      requireActive(accountId)
    }
  }

  private suspend fun cachedRecordsForAvatarKeys(accountId: String): Set<String> =
    cachedRecordIds(accountId).mapNotNull { id -> cachedListEntry(accountId, id)?.let(::avatarCacheKey) }.toSet()

  private fun avatarCacheKey(entry: RecordListEntry): String? = entry.requesterAvatar.url?.let {
    "avatar-${avatarSource(it)}-${entry.requesterAvatar.revision ?: "legacy"}"
  }

  suspend fun removeRecords(accountId: String, recordIds: Set<String>): Unit = accountLock.withLock {
    prepareRead(accountId)
    try { cache.deleteRecords(accountId, recordIds) }
    catch (_: RecordCacheException) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
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
    val result = load(accountId, recordId, CachedRecordPart.LIST)?.let { serialized ->
      decode(serialized) {
        json.decodeFromString<CachedListEntry>(it).also { cached ->
          check(cached.schemaVersion == 1 && cached.entry.recordId == recordId)
        }.entry
      }
    }
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
    catch (_: RecordCacheException) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
    requireActive(accountId)
  }

  private fun requireActive(accountId: String) {
    if (!isActiveAccount(accountId)) throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
  }

  private suspend fun prepare(accountId: String) {
    if (!activateOwner) { prepareRead(accountId); return }
    if (!isActiveAccount(accountId)) throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
    try { account.activate(accountId) }
    catch (_: RecordCacheException) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
    if (!isActiveAccount(accountId)) throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
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

  override fun close() {
    try { remote.close() } finally { database.close() }
  }

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
