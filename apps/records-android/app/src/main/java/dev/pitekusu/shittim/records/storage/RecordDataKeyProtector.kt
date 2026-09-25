package dev.pitekusu.shittim.records.storage

import androidx.annotation.WorkerThread
import org.bouncycastle.crypto.AsymmetricCipherKeyPair
import org.bouncycastle.crypto.hpke.HPKE
import org.bouncycastle.crypto.params.MLKEMPrivateKeyParameters

internal class RecordKeyProtectionException : Exception("record_key_protection_failed")

/** Wraps a 256-bit record data key; the ML-KEM private seed stays in [KeystorePrivateKeyStore]. */
@WorkerThread
internal class RecordDataKeyProtector(private val privateKeys: KeystorePrivateKeyStore) {
  fun wrap(accountId: String, recordId: String, dataKey: ByteArray): ByteArray = guarded {
    validateIds(accountId, recordId)
    check(dataKey.size == DATA_KEY_BYTES)
    val hpke = suite()
    val pair = loadPair(accountId, hpke) ?: generatePair(accountId, hpke)
    try {
      val sender = hpke.setupBaseS(pair.getPublic(), INFO)
      val encapsulation = sender.encapsulation
      val ciphertext = sender.seal(aad(accountId, recordId), dataKey)
      check(encapsulation.size == hpke.encSize && ciphertext.size == CIPHERTEXT_BYTES)
      byteArrayOf(VERSION) + encapsulation + ciphertext
    } finally {
      destroy(pair)
    }
  }

  fun unwrap(accountId: String, recordId: String, wrappedKey: ByteArray): ByteArray = guarded {
    validateIds(accountId, recordId)
    val hpke = suite()
    val encSize = hpke.encSize
    check(wrappedKey.size == 1 + encSize + CIPHERTEXT_BYTES && wrappedKey[0] == VERSION)
    // Decryption must never create a replacement key for missing or damaged storage.
    val pair = loadPair(accountId, hpke) ?: throw RecordKeyProtectionException()
    try {
      val recipient = hpke.setupBaseR(wrappedKey.copyOfRange(1, 1 + encSize), pair, INFO)
      recipient.open(aad(accountId, recordId), wrappedKey.copyOfRange(1 + encSize, wrappedKey.size))
        .also {
          if (it.size != DATA_KEY_BYTES) {
            it.fill(0)
            throw RecordKeyProtectionException()
          }
        }
    } finally {
      destroy(pair)
    }
  }

  private fun loadPair(accountId: String, hpke: HPKE): AsymmetricCipherKeyPair? {
    val stored = privateKeys.read(accountId) ?: return null
    try {
      check(stored.size == 1 + PRIVATE_SEED_BYTES && stored[0] == VERSION)
      val seed = stored.copyOfRange(1, stored.size)
      return try {
        hpke.deserializePrivateKey(seed, null)
      } finally {
        seed.fill(0)
      }
    } finally {
      stored.fill(0)
    }
  }

  private fun generatePair(accountId: String, hpke: HPKE): AsymmetricCipherKeyPair {
    val pair = hpke.generatePrivateKey()
    try {
      val seed = hpke.serializePrivateKey(pair.getPrivate())
      try {
        check(seed.size == PRIVATE_SEED_BYTES)
        val stored = byteArrayOf(VERSION) + seed
        try {
          privateKeys.save(accountId, stored)
        } finally {
          stored.fill(0)
        }
      } finally {
        seed.fill(0)
      }
      return pair
    } catch (error: Exception) {
      destroy(pair)
      throw error
    }
  }

  private fun validateIds(accountId: String, recordId: String) {
    check(accountId.matches(OPAQUE_ID) && recordId.matches(OPAQUE_ID))
  }

  private fun aad(accountId: String, recordId: String): ByteArray =
    "$AAD_PREFIX|$accountId|$recordId".toByteArray(Charsets.US_ASCII)

  private fun suite(): HPKE = HPKE(
    HPKE.mode_base, HPKE.kem_ML_KEM_768, HPKE.kdf_HKDF_SHA256, HPKE.aead_AES_GCM256,
  )

  private fun destroy(pair: AsymmetricCipherKeyPair) {
    (pair.getPrivate() as? MLKEMPrivateKeyParameters)?.destroy()
  }

  private fun <T> guarded(operation: () -> T): T = try {
    operation()
  } catch (_: Exception) {
    // Neither identifiers, key bytes nor provider error details belong in logs or UI.
    throw RecordKeyProtectionException()
  }

  private companion object {
    val OPAQUE_ID = Regex("[A-Za-z0-9_-]{1,128}")
    val INFO = "shittim-records-offline-data-key/v1".toByteArray(Charsets.US_ASCII)
    const val AAD_PREFIX = "shittim-records|data-key|v1"
    const val VERSION: Byte = 1
    const val PRIVATE_SEED_BYTES = 64
    const val DATA_KEY_BYTES = 32
    const val CIPHERTEXT_BYTES = DATA_KEY_BYTES + 16 // AES-GCM authentication tag.
  }
}
