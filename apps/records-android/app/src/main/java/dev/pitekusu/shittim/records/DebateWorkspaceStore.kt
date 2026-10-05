package dev.pitekusu.shittim.records

import android.content.Context
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import java.io.Closeable
import kotlinx.coroutines.sync.withLock
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** One atomic encrypted workspace: frozen requests are never silently rekeyed or edited. */
@Serializable
internal class DebateWorkspace(val schemaVersion: Int = 1, val draft: String = "",
  val requestId: String? = null, val frozenQuestion: String? = null,
  val receipt: DebateRequest? = null) {
  fun validate() {
    check(schemaVersion == 1 && draft.codePointCount(0, draft.length) <= 1000)
    check((requestId == null) == (frozenQuestion == null))
    check(requestId == null || validDebateRequestId(requestId))
    check(frozenQuestion == null || validDebateQuestion(frozenQuestion))
    receipt?.let { it.validate(); check(it.requestId == requestId && it.question == frozenQuestion) }
  }
  override fun toString(): String = "DebateWorkspace(<redacted>)"
}

internal class DebateWorkspaceStore(private val database: EncryptedRecordsDatabase,
  private val cache: EncryptedRecordStore, private val owner: RecordCacheAccount,
  private val authorized: (String) -> Boolean) : Closeable {
  private val json = Json { encodeDefaults = true; ignoreUnknownKeys = true }

  suspend fun load(accountId: String): DebateWorkspace = RecordCacheAccount.lock.withLock {
    guard(accountId)
    val bytes = cache.load(accountId, WORKSPACE_ID, CachedRecordPart.DEBATE_WORKSPACE)
    val result = if (bytes == null) DebateWorkspace() else try {
      json.decodeFromString<DebateWorkspace>(bytes.decodeToString()).also { it.validate() }
    } finally { bytes.fill(0) }
    guard(accountId)
    result
  }

  suspend fun save(accountId: String, value: DebateWorkspace) = RecordCacheAccount.lock.withLock {
    guard(accountId)
    value.validate()
    val bytes = json.encodeToString(value).encodeToByteArray()
    try { cache.save(accountId, WORKSPACE_ID, CachedRecordPart.DEBATE_WORKSPACE, bytes) }
    finally { bytes.fill(0) }
    guard(accountId)
  }

  private suspend fun guard(accountId: String) {
    if (!authorized(accountId)) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
    owner.requireOwner(accountId)
    if (!authorized(accountId)) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
  }
  override fun close() { database.close() }

  companion object {
    private const val WORKSPACE_ID = "debate-workspace-v1"
    fun open(context: Context, authorized: (String) -> Boolean): DebateWorkspaceStore {
      val database = EncryptedRecordsDatabase.open(context)
      try {
        val keys = KeystorePrivateKeyStore(context)
        return DebateWorkspaceStore(database, EncryptedRecordStore(database, RecordDataKeyProtector(keys)),
          RecordCacheAccount(context, keys, database), authorized)
      } catch (error: Exception) { database.close(); throw error }
    }
  }
}
