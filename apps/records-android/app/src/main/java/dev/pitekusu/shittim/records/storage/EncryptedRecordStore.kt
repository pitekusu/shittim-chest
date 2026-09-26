package dev.pitekusu.shittim.records.storage

import android.util.Base64
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

internal enum class CachedRecordPart(val code: String) {
  LIST("list"), DETAIL("detail"), SYNC_PROGRESS("sync-progress")
}

internal class RecordCacheException : Exception("record_cache_unavailable")

/** Encrypts before Room sees a record; callers own and may erase returned plaintext bytes. */
internal class EncryptedRecordStore(
  private val database: EncryptedRecordsDatabase,
  private val keyProtector: RecordDataKeyProtector,
) {
  private val random = SecureRandom()

  suspend fun save(accountId: String, recordId: String, part: CachedRecordPart, payload: ByteArray): Unit = guarded {
    validateIds(accountId, recordId)
    check(payload.size in 1..MAX_PAYLOAD_BYTES)
    val dataKey = ByteArray(DATA_KEY_BYTES).also(random::nextBytes)
    try {
      val encrypted = encrypt(accountId, recordId, part, dataKey, payload)
      val wrapped = keyProtector.wrap(accountId, recordId, dataKey)
      database.records().put(EncryptedRecordRow(accountKey(accountId), recordId, part.code, wrapped, encrypted))
    } finally {
      dataKey.fill(0)
    }
  }

  suspend fun load(accountId: String, recordId: String, part: CachedRecordPart): ByteArray? = guarded {
    validateIds(accountId, recordId)
    val row = database.records().get(accountKey(accountId), recordId, part.code) ?: return@guarded null
    val dataKey = keyProtector.unwrap(accountId, recordId, row.wrappedKey)
    try {
      decrypt(accountId, recordId, part, dataKey, row.encryptedPayload)
    } finally {
      dataKey.fill(0)
    }
  }

  suspend fun recordIds(accountId: String, part: CachedRecordPart): List<String> = guarded {
    check(accountId.matches(OPAQUE_ID))
    database.records().recordIds(accountKey(accountId), part.code)
      .also { ids -> check(ids.all(OPAQUE_ID::matches)) }
  }

  suspend fun deleteRecords(accountId: String, recordIds: Collection<String>): Unit = guarded {
    recordIds.forEach { validateIds(accountId, it) }
    // Stay below SQLite's bind-variable limit; each delete removes list and detail together.
    recordIds.chunked(200).forEach { database.records().deleteRecords(accountKey(accountId), it) }
  }

  /** Removes ciphertext only; account lifecycle coordinates Keystore key invalidation. */
  suspend fun deleteAccount(accountId: String): Unit = guarded {
    check(accountId.matches(OPAQUE_ID))
    database.records().deleteAccount(accountKey(accountId))
  }

  private fun encrypt(accountId: String, recordId: String, part: CachedRecordPart,
    dataKey: ByteArray, payload: ByteArray): ByteArray {
    val iv = ByteArray(IV_BYTES).also(random::nextBytes)
    val cipher = Cipher.getInstance(TRANSFORMATION)
    cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(dataKey, "AES"), GCMParameterSpec(TAG_BITS, iv))
    cipher.updateAAD(aad(accountId, recordId, part))
    return byteArrayOf(VERSION) + iv + cipher.doFinal(payload)
  }

  private fun decrypt(accountId: String, recordId: String, part: CachedRecordPart,
    dataKey: ByteArray, encrypted: ByteArray): ByteArray {
    check(encrypted.size in MIN_ENVELOPE_BYTES..MAX_ENVELOPE_BYTES && encrypted[0] == VERSION)
    val cipher = Cipher.getInstance(TRANSFORMATION)
    cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(dataKey, "AES"),
      GCMParameterSpec(TAG_BITS, encrypted, 1, IV_BYTES))
    cipher.updateAAD(aad(accountId, recordId, part))
    return cipher.doFinal(encrypted, 1 + IV_BYTES, encrypted.size - 1 - IV_BYTES)
      .also {
        if (it.size !in 1..MAX_PAYLOAD_BYTES) {
          it.fill(0)
          throw RecordCacheException()
        }
      }
  }

  private fun validateIds(accountId: String, recordId: String) {
    check(accountId.matches(OPAQUE_ID) && recordId.matches(OPAQUE_ID))
  }

  private fun aad(accountId: String, recordId: String, part: CachedRecordPart): ByteArray =
    "$AAD_PREFIX|$accountId|$recordId|${part.code}".toByteArray(Charsets.US_ASCII)

  // A pseudonymous index, not an encryption key. The raw account ID never becomes a SQL column.
  private fun accountKey(accountId: String): String = Base64.encodeToString(
    MessageDigest.getInstance("SHA-256")
      .digest("shittim-records|account-index|v1|$accountId".toByteArray(Charsets.US_ASCII)),
    Base64.URL_SAFE or Base64.NO_PADDING or Base64.NO_WRAP,
  )

  private suspend fun <T> guarded(operation: suspend () -> T): T = try {
    withContext(Dispatchers.IO) { operation() }
  } catch (error: CancellationException) {
    throw error
  } catch (_: Exception) {
    // Database, provider, IDs, and payload never enter an exception or log.
    throw RecordCacheException()
  }

  private companion object {
    val OPAQUE_ID = Regex("[A-Za-z0-9_-]{1,128}")
    const val AAD_PREFIX = "shittim-records|payload|v1"
    const val TRANSFORMATION = "AES/GCM/NoPadding"
    const val VERSION: Byte = 1
    const val DATA_KEY_BYTES = 32
    const val IV_BYTES = 12
    const val TAG_BITS = 128
    // Keep the complete encrypted row below older framework CursorWindow limits.
    const val MAX_PAYLOAD_BYTES = 1024 * 1024
    const val MIN_ENVELOPE_BYTES = 1 + IV_BYTES + 1 + TAG_BITS / 8
    const val MAX_ENVELOPE_BYTES = 1 + IV_BYTES + MAX_PAYLOAD_BYTES + TAG_BITS / 8
  }
}
