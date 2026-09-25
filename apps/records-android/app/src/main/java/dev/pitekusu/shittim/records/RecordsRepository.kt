package dev.pitekusu.shittim.records

import android.content.Context
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheException
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import java.io.Closeable
import java.time.Instant
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.serialization.KSerializer
import kotlinx.serialization.Serializable
import kotlinx.serialization.descriptors.PrimitiveKind
import kotlinx.serialization.descriptors.PrimitiveSerialDescriptor
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.Json

/** Keeps the existing authenticated API boundary and writes only encrypted, validated records. */
internal class RecordsRepository(
  private val remote: RecordsReadClient,
  private val cache: EncryptedRecordStore,
  private val database: EncryptedRecordsDatabase,
) : Closeable {
  private val json = Json { encodeDefaults = true }

  suspend fun recentRecords(token: String, accountId: String, cursor: String?): RecordListPage {
    val page = remote.recentRecords(token, cursor)
    page.items.forEach { entry ->
      save(accountId, entry.recordId, CachedRecordPart.LIST) {
        json.encodeToString(CachedListEntry(1, entry))
      }
    }
    return page
  }

  suspend fun record(token: String, accountId: String, recordId: String): RecordReadResult {
    val result = remote.firstRecord(token, "/records/$recordId")
    if (result is RecordReadResult.Found) {
      save(accountId, recordId, CachedRecordPart.DETAIL) {
        json.encodeToString(CachedDetail(1, result.preview))
      }
    }
    return result
  }

  // C30 gates offline use by the authenticated account and the 90-day deadline.
  suspend fun cachedListEntry(accountId: String, recordId: String): RecordListEntry? =
    load(accountId, recordId, CachedRecordPart.LIST)?.let { serialized ->
      decode(serialized) {
        json.decodeFromString<CachedListEntry>(it).also { cached ->
          check(cached.schemaVersion == 1 && cached.entry.recordId == recordId)
        }.entry
      }
    }

  suspend fun cachedRecord(accountId: String, recordId: String): RecordPreview? =
    load(accountId, recordId, CachedRecordPart.DETAIL)?.let { serialized ->
      decode(serialized) {
        json.decodeFromString<CachedDetail>(it).also { cached ->
          check(cached.schemaVersion == 1)
        }.preview
      }
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
    fun open(context: Context): RecordsRepository {
      val database = EncryptedRecordsDatabase.open(context)
      try {
        val cache = EncryptedRecordStore(database, RecordDataKeyProtector(KeystorePrivateKeyStore(context)))
        return RecordsRepository(RecordsReadClient(), cache, database)
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

internal object CachedRecordInstantSerializer : KSerializer<Instant> {
  override val descriptor = PrimitiveSerialDescriptor("CachedRecordInstant", PrimitiveKind.STRING)
  override fun serialize(encoder: Encoder, value: Instant) = encoder.encodeString(value.toString())
  override fun deserialize(decoder: Decoder): Instant = Instant.parse(decoder.decodeString())
}
