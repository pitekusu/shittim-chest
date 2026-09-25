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

/** One private key file is shared by the app, so switch accounts before any cache operation. */
internal class RecordCacheAccount(
  context: Context,
  private val privateKeys: KeystorePrivateKeyStore,
  private val database: EncryptedRecordsDatabase,
) {
  private val marker = AtomicFile(File(context.noBackupFilesDir, "records-cache-account.v1"))

  suspend fun activate(accountId: String): Unit = try {
    withContext(Dispatchers.IO) {
      check(ACCOUNT_ID.matches(accountId))
      val expected = accountKey(accountId)
      val previous = readMarker()
      if (previous != expected) {
        if (previous != null) {
          // Invalidate the old private key before deleting its ciphertext.
          privateKeys.clear()
          database.records().deleteAll()
        }
        writeMarker(expected)
      }
    }
  } catch (error: CancellationException) {
    throw error
  } catch (_: Exception) {
    throw RecordCacheException()
  }

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

  private companion object {
    val ACCOUNT_ID = Regex("[A-Za-z0-9_-]{43}")
  }
}
