package dev.pitekusu.shittim.records.auth

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import androidx.annotation.WorkerThread
import java.io.DataInputStream
import java.io.File
import java.io.FileNotFoundException
import java.nio.ByteBuffer
import java.security.KeyStore
import java.time.Instant
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Blocking, single-process storage. Reading does not renew expiry or authorize a session. */
@WorkerThread
internal class KeystoreTokenStore(context: Context) {
  private val file = AtomicFile(File(context.noBackupFilesDir, "mobile-session.v1"))
  private val logoutIntent = AtomicFile(File(context.noBackupFilesDir, "mobile-session-logout.v1"))
  private val keyAlias = "${context.packageName}.mobile-session.v1"

  init {
    require(!context.isDeviceProtectedStorage) { "credential_protected_storage_required" }
  }

  fun read(): StoredToken? = guarded {
    val envelope = try {
      readEnvelope()
    } catch (error: FileNotFoundException) {
      if (file.baseFile.exists()) throw error
      return@guarded null
    }
    val version = envelope[0]
    check(envelope.size == envelopeSize(version))
    // A lost or invalidated key is not silently replaced while reading old ciphertext.
    val key = keyStore().getKey(keyAlias, null) as? SecretKey ?: throw TokenStorageException()
    val cipher = Cipher.getInstance(TRANSFORMATION)
    cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(TAG_BITS, envelope, 1, IV_BYTES))
    cipher.updateAAD(keyAlias.toByteArray(Charsets.UTF_8) + version)
    val plaintext = cipher.doFinal(envelope, 1 + IV_BYTES, envelope.size - 1 - IV_BYTES)
    try {
      val payload = ByteBuffer.wrap(plaintext)
      val expiry = Instant.ofEpochSecond(payload.long)
      val token = ByteArray(TOKEN_BYTES).also(payload::get)
      try {
        val authorization = if (version == AUTHORIZED_VERSION) {
          val account = ByteArray(TOKEN_BYTES).also(payload::get).toString(Charsets.US_ASCII)
          CacheAuthorization(account, Instant.ofEpochSecond(payload.long), Instant.ofEpochSecond(payload.long))
        } else null
        StoredToken(token.toString(Charsets.US_ASCII), expiry, authorization)
      } finally {
        token.fill(0)
      }
    } finally {
      plaintext.fill(0)
    }
  }

  fun save(token: StoredToken): Unit = guarded {
    check(!hasLogoutIntent())
    val authorization = token.cacheAuthorization
    val version = if (authorization == null) VERSION else AUTHORIZED_VERSION
    val cipher = Cipher.getInstance(TRANSFORMATION)
    cipher.init(Cipher.ENCRYPT_MODE, encryptionKey()) // Keystore chooses a fresh random IV.
    cipher.updateAAD(keyAlias.toByteArray(Charsets.UTF_8) + version)
    check(cipher.iv.size == IV_BYTES)
    val payload = ByteBuffer.allocate(payloadSize(version))
      .putLong(token.expiresAt.epochSecond)
      .put(token.accessToken.toByteArray(Charsets.US_ASCII))
    if (authorization != null) {
      payload.put(authorization.accountId.toByteArray(Charsets.US_ASCII))
        .putLong(authorization.verifiedAt.epochSecond).putLong(authorization.expiresAt.epochSecond)
    }
    val plaintext = payload.array()
    val ciphertext = try {
      cipher.doFinal(plaintext)
    } finally {
      plaintext.fill(0)
    }
    val envelope = byteArrayOf(version) + cipher.iv + ciphertext
    val output = file.startWrite()
    try {
      output.write(envelope) // Plaintext never reaches the file, including temporary files.
      file.finishWrite(output)
      // AtomicFile reports some sync/rename errors only to the platform log.
      check(readEnvelope().contentEquals(envelope))
    } catch (error: Exception) {
      file.failWrite(output)
      throw error
    }
  }

  /** Persist before record/token deletion; any surviving intent prohibits session restoration. */
  fun beginLogout(): Unit = guarded {
    val output = logoutIntent.startWrite()
    try {
      output.write(1) // No credential or user data; this is only a deletion intent.
      logoutIntent.finishWrite(output)
      logoutIntent.openRead().use { check(it.read() == 1 && it.read() == -1) }
    } catch (error: Exception) {
      logoutIntent.failWrite(output)
      throw error
    }
  }

  fun isLogoutPending(): Boolean = guarded { hasLogoutIntent() }

  /** Revocation locks offline data without erasing it or resurrecting another login. */
  fun invalidateCacheAuthorization(expectedToken: String): Unit = guarded {
    if (!hasLogoutIntent()) {
      val current = read()
      if (current?.accessToken == expectedToken) save(StoredToken(current.accessToken, current.expiresAt))
    }
  }

  /** Erase credentials even when record cleanup fails; keep its independent durable intent. */
  fun clear(): Unit = guarded {
    val keys = keyStore()
    keys.deleteEntry(keyAlias) // Invalidate any remaining ciphertext before removing the files.
    file.delete()
    check(!keys.containsAlias(keyAlias))
    check(listOf("", ".new", ".bak").none { File(file.baseFile.path + it).exists() })
  }

  /** Called only after record deletion succeeds. Missing token alone is not completed logout. */
  fun completeLogout(): Unit = guarded {
    check(!keyStore().containsAlias(keyAlias))
    check(listOf("", ".new", ".bak").none { File(file.baseFile.path + it).exists() })
    logoutIntent.delete()
    check(!hasLogoutIntent())
  }

  private fun hasLogoutIntent(): Boolean = listOf("", ".new", ".bak")
    .any { File(logoutIntent.baseFile.path + it).exists() }

  private fun readEnvelope(): ByteArray = DataInputStream(file.openRead()).use { input ->
    // Read the authenticated version with a strict bound; accept the deployed v1 format.
    val version = input.readByte()
    ByteArray(envelopeSize(version)).also { bytes ->
      bytes[0] = version
      input.readFully(bytes, 1, bytes.size - 1)
      check(input.read() == -1)
    }
  }

  private fun encryptionKey(): SecretKey {
    val keys = keyStore()
    if (keys.containsAlias(keyAlias)) {
      return keys.getKey(keyAlias, null) as? SecretKey ?: throw TokenStorageException()
    }
    check(listOf("", ".new", ".bak").none { File(file.baseFile.path + it).exists() })
    return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE).apply {
      init(
        KeyGenParameterSpec.Builder(
          keyAlias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
        )
          .setKeySize(256)
          .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
          .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
          .setRandomizedEncryptionRequired(true)
          .build()
      )
    }.generateKey()
  }

  private fun keyStore(): KeyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }

  private fun <T> guarded(operation: () -> T): T = synchronized(lock) {
    try {
      operation()
    } catch (_: Exception) {
      throw TokenStorageException()
    }
  }

  private companion object {
    // Covers all instances, not just one Activity's store; no multi-process consumer exists.
    val lock = Any()
    const val KEYSTORE = "AndroidKeyStore"
    const val TRANSFORMATION = "AES/GCM/NoPadding"
    const val VERSION: Byte = 1
    const val AUTHORIZED_VERSION: Byte = 2
    const val TOKEN_BYTES = 43
    const val IV_BYTES = 12
    const val TAG_BITS = 128
    const val PAYLOAD_BYTES = Long.SIZE_BYTES + TOKEN_BYTES
    fun payloadSize(version: Byte): Int = when (version) {
      VERSION -> PAYLOAD_BYTES
      AUTHORIZED_VERSION -> PAYLOAD_BYTES + TOKEN_BYTES + 2 * Long.SIZE_BYTES
      else -> throw TokenStorageException()
    }
    fun envelopeSize(version: Byte): Int = 1 + IV_BYTES + payloadSize(version) + TAG_BITS / 8
  }
}
