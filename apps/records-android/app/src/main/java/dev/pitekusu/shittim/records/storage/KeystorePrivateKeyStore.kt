package dev.pitekusu.shittim.records.storage

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import androidx.annotation.WorkerThread
import java.io.DataInputStream
import java.io.File
import java.io.FileNotFoundException
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal class PrivateKeyStorageException : Exception("private_key_storage_unavailable")

/** Stores one account's encoded private key; callers must erase returned bytes after use. */
@WorkerThread
internal class KeystorePrivateKeyStore(context: Context) {
  private val file = AtomicFile(File(context.noBackupFilesDir, "records-private-key.v1"))
  private val keyAlias = "${context.packageName}.records-private-key.v1"
  private val associatedData = keyAlias.toByteArray(Charsets.UTF_8) + VERSION

  init {
    require(!context.isDeviceProtectedStorage) { "credential_protected_storage_required" }
  }

  fun read(accountId: String): ByteArray? = guarded {
    validateAccount(accountId)
    val envelope = try {
      readEnvelope()
    } catch (error: FileNotFoundException) {
      // A missing ciphertext with an existing key is not a fresh installation.
      if (hasFiles() || keyStore().containsAlias(keyAlias)) throw error
      return@guarded null
    }
    check(envelope[0] == VERSION)
    val key = keyStore().getKey(keyAlias, null) as? SecretKey ?: throw PrivateKeyStorageException()
    val cipher = Cipher.getInstance(TRANSFORMATION)
    cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(TAG_BITS, envelope, 1, IV_BYTES))
    cipher.updateAAD(aad(accountId))
    cipher.doFinal(envelope, 1 + IV_BYTES, envelope.size - 1 - IV_BYTES).also {
      if (it.size !in 1..MAX_KEY_BYTES) {
        it.fill(0)
        throw PrivateKeyStorageException()
      }
    }
  }

  fun save(accountId: String, encodedPrivateKey: ByteArray): Unit = guarded {
    validateAccount(accountId)
    check(encodedPrivateKey.size in 1..MAX_KEY_BYTES)
    // Rewriting a different private key would strand records encrypted for the old one.
    val existing = read(accountId)
    existing?.let {
      try {
        check(it.contentEquals(encodedPrivateKey))
      } finally {
        it.fill(0)
      }
    }
    try {
      val cipher = Cipher.getInstance(TRANSFORMATION)
      cipher.init(Cipher.ENCRYPT_MODE, encryptionKey()) // Keystore supplies a new random IV.
      cipher.updateAAD(aad(accountId))
      check(cipher.iv.size == IV_BYTES)
      val envelope = byteArrayOf(VERSION) + cipher.iv + cipher.doFinal(encodedPrivateKey)
      val output = file.startWrite()
      try {
        output.write(envelope)
        file.finishWrite(output)
        // AtomicFile can report some sync/rename failures only to the platform log.
        check(readEnvelope().contentEquals(envelope))
      } catch (error: Exception) {
        file.failWrite(output)
        throw error
      }
    } catch (error: Exception) {
      if (existing == null && !file.baseFile.exists()) {
        // Only an uncommitted first write may discard the key it just created.
        // If cleanup fails, the next read/save remains blocked until explicit clear.
        try {
          keyStore().deleteEntry(keyAlias)
          file.delete()
        } catch (_: Exception) { /* Keep the original failure category. */ }
      }
      throw error
    }
  }

  fun clear(): Unit = guarded {
    val keys = keyStore()
    keys.deleteEntry(keyAlias) // Invalidate any surviving ciphertext first.
    file.delete()
    check(!keys.containsAlias(keyAlias) && !hasFiles())
  }

  private fun aad(accountId: String): ByteArray =
    associatedData + 0.toByte() + accountId.toByteArray(Charsets.US_ASCII)

  private fun validateAccount(accountId: String) {
    check(accountId.matches(ACCOUNT_ID))
  }

  private fun hasFiles(): Boolean = listOf("", ".new", ".bak")
    .any { File(file.baseFile.path + it).exists() }

  private fun readEnvelope(): ByteArray = DataInputStream(file.openRead()).use { input ->
    val bytes = ByteArray(MAX_ENVELOPE_BYTES + 1)
    var size = 0
    while (size < bytes.size) {
      val count = input.read(bytes, size, bytes.size - size)
      if (count <= 0) break
      size += count
    }
    check(size in MIN_ENVELOPE_BYTES..MAX_ENVELOPE_BYTES)
    bytes.copyOf(size)
  }

  private fun encryptionKey(): SecretKey {
    val keys = keyStore()
    if (keys.containsAlias(keyAlias)) {
      return keys.getKey(keyAlias, null) as? SecretKey ?: throw PrivateKeyStorageException()
    }
    check(!hasFiles())
    return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE).apply {
      init(KeyGenParameterSpec.Builder(
        keyAlias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
      ).setKeySize(256)
        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
        .setRandomizedEncryptionRequired(true)
        .build())
    }.generateKey()
  }

  private fun keyStore(): KeyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }

  private fun <T> guarded(operation: () -> T): T = synchronized(lock) {
    try {
      operation()
    } catch (_: Exception) {
      // Neither the account identifier nor private-key bytes enter an exception or log.
      throw PrivateKeyStorageException()
    }
  }

  private companion object {
    val lock = Any() // All store instances in this single-process app share one file and key.
    val ACCOUNT_ID = Regex("[A-Za-z0-9_-]{1,128}")
    const val KEYSTORE = "AndroidKeyStore"
    const val TRANSFORMATION = "AES/GCM/NoPadding"
    const val VERSION: Byte = 1
    const val IV_BYTES = 12
    const val TAG_BITS = 128
    const val MAX_KEY_BYTES = 16 * 1024
    const val MIN_ENVELOPE_BYTES = 1 + IV_BYTES + 1 + TAG_BITS / 8
    const val MAX_ENVELOPE_BYTES = 1 + IV_BYTES + MAX_KEY_BYTES + TAG_BITS / 8
  }
}
