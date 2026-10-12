package dev.pitekusu.shittim.records

import android.content.Context
import android.net.Uri
import android.graphics.BitmapFactory
import java.util.UUID
import dev.pitekusu.shittim.records.storage.EncryptedRecordImageStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import dev.pitekusu.shittim.records.storage.StorageNamespace
import java.io.Closeable
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** One atomic encrypted workspace: frozen requests are never silently rekeyed or edited. */
@Serializable
internal class DebateWorkspace(val schemaVersion: Int = 1, val draft: String = "",
  val requestId: String? = null, val frozenQuestion: String? = null,
  val receipt: DebateRequest? = null, val attachments: List<DraftAttachment> = emptyList()) {
  fun validate() {
    check(schemaVersion == 1 && draft.codePointCount(0, draft.length) <= 1000)
    check((requestId == null) == (frozenQuestion == null))
    check(requestId == null || validDebateRequestId(requestId))
    check(frozenQuestion == null || validDebateQuestion(frozenQuestion))
    check(attachments.size <= MAX_ATTACHMENT_COUNT && attachments.map { it.attachmentId }.distinct().size == attachments.size)
    attachments.forEach(DraftAttachment::validate)
    receipt?.let { it.validate(); check(it.requestId == requestId && it.question == frozenQuestion && it.attachmentIds == attachments.map { image -> image.attachmentId }) }
  }
  override fun toString(): String = "DebateWorkspace(<redacted>)"
}

internal class DebateWorkspaceStore(private val database: EncryptedRecordsDatabase,
  private val cache: EncryptedRecordStore, private val owner: RecordCacheAccount,
  private val authorized: (String) -> Boolean,
  private val context: Context, private val images: EncryptedRecordImageStore,
  private val boundAccountId: String? = null) : Closeable {
  private val json = Json { encodeDefaults = true; ignoreUnknownKeys = true }

  suspend fun load(accountId: String): DebateWorkspace = RecordCacheAccount.lock.withLock {
    guard(accountId)
    val bytes = cache.load(accountId, WORKSPACE_ID, CachedRecordPart.DEBATE_WORKSPACE)
    val result = if (bytes == null) DebateWorkspace() else try {
      json.decodeFromString<DebateWorkspace>(bytes.decodeToString()).also { it.validate() }
    } finally { bytes.fill(0) }
    pruneDraft(accountId, result)
    guard(accountId)
    result
  }

  suspend fun save(accountId: String, value: DebateWorkspace) = RecordCacheAccount.lock.withLock {
    guard(accountId)
    value.validate()
    val bytes = json.encodeToString(value).encodeToByteArray()
    try { cache.save(accountId, WORKSPACE_ID, CachedRecordPart.DEBATE_WORKSPACE, bytes) }
    finally { bytes.fill(0) }
    pruneDraft(accountId, value)
    guard(accountId)
  }

  suspend fun importImage(accountId: String, uri: Uri): DraftAttachment {
    RecordCacheAccount.lock.withLock { guard(accountId) }
    val bytes = try { withContext(Dispatchers.IO) {
      check(uri.scheme == "content")
      context.contentResolver.openInputStream(uri)?.use { input ->
        val buffer = ByteArray(MAX_ATTACHMENT_BYTES + 1)
        try {
          var count = 0
          while (count < buffer.size) {
            val next = input.read(buffer, count, buffer.size - count)
            if (next <= 0) break
            count += next
          }
          check(count in 1..MAX_ATTACHMENT_BYTES)
          buffer.copyOf(count)
        } finally { buffer.fill(0) }
      } ?: throw RecordImageException(RecordImageFailure.INVALID)
    } } catch (error: kotlin.coroutines.cancellation.CancellationException) { throw error }
    catch (_: Exception) { throw RecordImageException(RecordImageFailure.INVALID) }
    return try { saveImageBytes(accountId, bytes) }
    finally { bytes.fill(0) }
  }

  /** Sample assets use the same validation and encrypted storage as Photo Picker imports. */
  suspend fun importBytes(accountId: String, bytes: ByteArray): DraftAttachment {
    if (bytes.size !in 1..MAX_ATTACHMENT_BYTES) throw RecordImageException(RecordImageFailure.INVALID)
    val privateBytes = bytes.copyOf()
    return try { saveImageBytes(accountId, privateBytes) }
    finally { privateBytes.fill(0) }
  }

  private suspend fun saveImageBytes(accountId: String, bytes: ByteArray): DraftAttachment {
    RecordCacheAccount.lock.withLock { guard(accountId) }
    try {
      val (width, height) = validateAttachmentBytes(bytes)
      val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
      BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
      val image = DraftAttachment(UUID.randomUUID().toString(), checkNotNull(bounds.outMimeType),
        bytes.size, attachmentDigest(bytes), width, height)
      RecordCacheAccount.lock.withLock {
        guard(accountId)
        images.save(accountId, "debate-draft", image.descriptor(), bytes)
        guard(accountId)
      }
      return image
    } catch (error: kotlin.coroutines.cancellation.CancellationException) { throw error }
    catch (error: RecordImageException) { throw error }
    catch (_: Exception) { throw RecordImageException(RecordImageFailure.INVALID) }
  }

  suspend fun image(accountId: String, image: DraftAttachment): ByteArray = RecordCacheAccount.lock.withLock {
    guard(accountId)
    val bytes = images.read(accountId, "debate-draft", image.descriptor())
      ?: throw RecordImageException(RecordImageFailure.STORAGE)
    try { guard(accountId) } catch (error: Exception) { bytes.fill(0); throw error }
    bytes
  }

  private suspend fun pruneDraft(accountId: String, value: DebateWorkspace) {
    try { images.retain(accountId, "debate-draft", value.attachments.map { it.descriptor() }) }
    catch (error: kotlin.coroutines.cancellation.CancellationException) { throw error }
    catch (_: RecordImageException) {
      // The workspace is already committed; failed orphan cleanup must not claim that save failed.
    }
  }

  private suspend fun guard(accountId: String) {
    if ((boundAccountId != null && boundAccountId != accountId) || !authorized(accountId))
      throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
    owner.requireOwner(accountId)
    if (!authorized(accountId)) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
  }
  override fun close() { database.close() }

  companion object {
    private const val WORKSPACE_ID = "debate-workspace-v1"
    fun open(context: Context, authorized: (String) -> Boolean,
      namespace: StorageNamespace = StorageNamespace.PRODUCTION): DebateWorkspaceStore =
      openScoped(context, null, authorized, namespace)

    fun open(context: Context, accountId: String, authorized: (String) -> Boolean,
      namespace: StorageNamespace = StorageNamespace.PRODUCTION): DebateWorkspaceStore {
      check(accountId.matches(Regex("[A-Za-z0-9_-]{43}")))
      return openScoped(context, accountId, authorized, namespace)
    }

    private fun openScoped(context: Context, accountId: String?, authorized: (String) -> Boolean,
      namespace: StorageNamespace): DebateWorkspaceStore {
      val database = EncryptedRecordsDatabase.open(context, namespace)
      try {
        val keys = KeystorePrivateKeyStore(context, namespace)
        return DebateWorkspaceStore(database, EncryptedRecordStore(database, RecordDataKeyProtector(keys)),
          RecordCacheAccount(context, keys, database, namespace), authorized, context.applicationContext,
          EncryptedRecordImageStore(context, namespace = namespace), accountId)
      } catch (error: Exception) { database.close(); throw error }
    }
  }
}
