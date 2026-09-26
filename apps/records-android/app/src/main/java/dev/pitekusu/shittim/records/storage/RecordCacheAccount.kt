package dev.pitekusu.shittim.records.storage

import android.content.Context
import android.util.AtomicFile
import java.io.DataInputStream
import java.io.File
import java.io.FileNotFoundException
import java.security.MessageDigest
import java.util.Base64
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** One private key file is shared by the app; callers hold [lock] for lifecycle and cache operations. */
internal class RecordCacheAccount(
  context: Context,
  private val privateKeys: KeystorePrivateKeyStore,
  private val database: EncryptedRecordsDatabase,
) {
  private val marker = AtomicFile(File(context.noBackupFilesDir, "records-cache-account.v1"))
  private val deletion = AtomicFile(File(context.noBackupFilesDir, "records-cache-delete.v1"))

  suspend fun activate(accountId: String): Unit = try {
    withContext(Dispatchers.IO) {
      check(ACCOUNT_ID.matches(accountId))
      // Recover interrupted/failed logout before granting either account a new permit.
      if (hasDeletionMarker()) clear()
      val expected = accountKey(accountId)
      val previous = readMarker()
      if (previous != expected) {
        if (previous != null) {
          // The same recoverable erasure is required for account switching and logout.
          clear()
        }
        writeMarker(expected)
      }
    }
  } catch (error: CancellationException) {
    throw error
  } catch (_: Exception) {
    throw RecordCacheException()
  }

  /** Expiry never calls this: explicit logout must invalidate keys before deleting ciphertext. */
  suspend fun clear(): Unit = try {
    withContext(Dispatchers.IO) {
      val output = deletion.startWrite()
      try {
        output.write(1) // No account or user data in the durable deletion intent.
        deletion.finishWrite(output)
        deletion.openRead().use { check(it.read() == 1 && it.read() == -1) }
      } catch (error: Exception) {
        deletion.failWrite(output)
        throw error
      }
      privateKeys.clear()
      database.records().deleteAll()
      marker.delete()
      check(listOf("", ".new", ".bak").none { File(marker.baseFile.path + it).exists() })
      deletion.delete()
      check(!hasDeletionMarker())
    }
  } catch (error: CancellationException) {
    throw error
  } catch (_: Exception) {
    throw RecordCacheException()
  }

  suspend fun requireOwner(accountId: String): Unit = try {
    withContext(Dispatchers.IO) { check(!hasDeletionMarker() && readMarker() == accountKey(accountId)) }
  } catch (error: CancellationException) {
    throw error
  } catch (_: Exception) {
    throw RecordCacheException()
  }

  private fun hasDeletionMarker(): Boolean = listOf("", ".new", ".bak")
    .any { File(deletion.baseFile.path + it).exists() }

  private fun readMarker(): String? = try {
    DataInputStream(marker.openRead()).use { input ->
      val bytes = ByteArray(43)
      input.readFully(bytes)
      check(input.read() == -1)
      bytes.toString(Charsets.US_ASCII).also { check(ACCOUNT_ID.matches(it)) }
    }
  } catch (_: FileNotFoundException) {
    null
  }

  private fun writeMarker(value: String) {
    val output = marker.startWrite()
    try {
      output.write(value.toByteArray(Charsets.US_ASCII))
      marker.finishWrite(output)
      check(readMarker() == value)
    } catch (error: Exception) {
      marker.failWrite(output)
      throw error
    }
  }

  private fun accountKey(accountId: String): String = Base64.getUrlEncoder().withoutPadding()
    .encodeToString(MessageDigest.getInstance("SHA-256")
      .digest("shittim-records|cache-owner|v1|$accountId".toByteArray(Charsets.US_ASCII)))

  companion object {
    val lock = Mutex() // Also covers Activity/repository instances and session lifecycle callbacks.
    private val ACCOUNT_ID = Regex("[A-Za-z0-9_-]{43}")

    suspend fun activate(context: Context, accountId: String) = update(context) { it.activate(accountId) }

    suspend fun clear(context: Context) = update(context) { it.clear() }

    private suspend fun update(context: Context, operation: suspend (RecordCacheAccount) -> Unit): Unit = try {
      lock.withLock {
        val database = EncryptedRecordsDatabase.open(context)
        try { operation(RecordCacheAccount(context, KeystorePrivateKeyStore(context), database)) }
        finally { database.close() }
      }
    } catch (error: CancellationException) {
      throw error
    } catch (_: Exception) {
      throw RecordCacheException()
    }
  }
}
