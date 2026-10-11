package dev.pitekusu.shittim.records.storage

import android.content.Context
import android.util.AtomicFile
import dev.pitekusu.shittim.records.MAX_ATTACHMENT_BYTES
import dev.pitekusu.shittim.records.RecordAttachment
import dev.pitekusu.shittim.records.RecordImageException
import dev.pitekusu.shittim.records.RecordImageFailure
import dev.pitekusu.shittim.records.attachmentDigest
import java.io.File
import java.io.FileNotFoundException
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** Original image bytes exceed Room's 1 MiB CursorWindow boundary. Callers hold the account lock. */
internal class EncryptedRecordImageStore(context: Context,
  private val protector: RecordDataKeyProtector = RecordDataKeyProtector(KeystorePrivateKeyStore(context)),
  private val availableBytes: () -> Long = { context.noBackupFilesDir.usableSpace }) {
  private val root = File(context.noBackupFilesDir, DIRECTORY)
  init { check(!context.isDeviceProtectedStorage) }

  suspend fun save(accountId: String, binding: String, image: RecordAttachment, bytes: ByteArray): Unit = guarded {
    image.validate()
    check(bytes.size == image.sizeBytes && attachmentDigest(bytes) == image.sha256)
    val id = identity(accountId, binding, image)
    val target = file(accountId, binding, image)
    check(target.baseFile.parentFile!!.mkdirs() || target.baseFile.parentFile!!.isDirectory)
    if (availableBytes() - image.sizeBytes - HEADER_BYTES - 16 < FREE_RESERVE_BYTES)
      throw RecordImageException(RecordImageFailure.STORAGE)
    val key = ByteArray(32).also(SecureRandom()::nextBytes)
    try {
      val wrapped = protector.wrap(accountId, id, key)
      check(wrapped.size == WRAPPED_BYTES)
      val iv = ByteArray(12).also(SecureRandom()::nextBytes)
      val cipher = Cipher.getInstance("AES/GCM/NoPadding")
      cipher.init(Cipher.ENCRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, iv))
      cipher.updateAAD(aad(accountId, binding, image))
      val envelope = byteArrayOf(1) + wrapped + iv + cipher.doFinal(bytes)
      val output = target.startWrite()
      try {
        output.write(envelope)
        target.finishWrite(output)
        check(readEnvelope(target).contentEquals(envelope))
      } catch (error: Exception) { target.failWrite(output); throw error }
    } finally { key.fill(0) }
  }

  suspend fun read(accountId: String, binding: String, image: RecordAttachment): ByteArray? = guarded {
    image.validate()
    val envelope = try { readEnvelope(file(accountId, binding, image)) }
    catch (_: FileNotFoundException) { return@guarded null }
    check(envelope[0] == 1.toByte())
    val key = protector.unwrap(accountId, identity(accountId, binding, image), envelope.copyOfRange(1, 1 + WRAPPED_BYTES))
    try {
      val cipher = Cipher.getInstance("AES/GCM/NoPadding")
      cipher.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"),
        GCMParameterSpec(128, envelope, 1 + WRAPPED_BYTES, 12))
      cipher.updateAAD(aad(accountId, binding, image))
      val bytes = cipher.doFinal(envelope, HEADER_BYTES, envelope.size - HEADER_BYTES)
      if (bytes.size != image.sizeBytes || attachmentDigest(bytes) != image.sha256) {
        bytes.fill(0)
        throw RecordImageException(RecordImageFailure.INVALID)
      }
      bytes
    } finally { key.fill(0) }
  }

  suspend fun retain(accountId: String, binding: String, images: List<RecordAttachment>): Unit = guarded {
    val group = group(accountId, binding)
    val wanted = images.map { identity(accountId, binding, it) }.toSet()
    group.listFiles().orEmpty().forEach { entry ->
      val name = entry.name.substringBefore('.')
      check(entry.isFile && name.matches(Regex("[A-Za-z0-9_-]{43}")))
      if (name !in wanted) AtomicFile(File(group, "$name.image")).delete()
    }
    check(group.listFiles().orEmpty().all { it.name.substringBefore('.') in wanted })
  }

  private fun file(account: String, binding: String, image: RecordAttachment) =
    AtomicFile(File(group(account, binding), "${identity(account, binding, image)}.image"))
  private fun group(account: String, binding: String): File {
    check(account.matches(Regex("[A-Za-z0-9_-]{1,128}")) && binding.matches(Regex("[A-Za-z0-9_-]{1,128}")))
    return File(root, hash("group|$account|$binding"))
  }
  private fun identity(account: String, binding: String, image: RecordAttachment): String =
    hash("image|$account|$binding|${image.attachmentId}|${image.sha256}")
  private fun hash(value: String) = Base64.getUrlEncoder().withoutPadding().encodeToString(
    MessageDigest.getInstance("SHA-256").digest(value.toByteArray(Charsets.US_ASCII)))
  private fun aad(account: String, binding: String, image: RecordAttachment) =
    "shittim-records|image|v1|$account|$binding|${image.attachmentId}|${image.sha256}".toByteArray(Charsets.US_ASCII)
  private fun readEnvelope(file: AtomicFile): ByteArray = file.openRead().use { input ->
    val buffer = ByteArray(MAX_ATTACHMENT_BYTES + HEADER_BYTES + 17)
    var size = 0
    while (size < buffer.size) {
      val count = input.read(buffer, size, buffer.size - size)
      if (count <= 0) break
      size += count
    }
    check(size in (HEADER_BYTES + 17)..(MAX_ATTACHMENT_BYTES + HEADER_BYTES + 16))
    buffer.copyOf(size)
  }
  private suspend fun <T> guarded(block: () -> T): T = try { withContext(Dispatchers.IO) { block() } }
  catch (error: CancellationException) { throw error }
  catch (error: RecordImageException) { throw error }
  catch (_: Exception) { throw RecordImageException(RecordImageFailure.STORAGE) }

  companion object {
    private const val DIRECTORY = "record-images-v1"
    private const val WRAPPED_BYTES = 1 + 1088 + 48
    private const val HEADER_BYTES = 1 + WRAPPED_BYTES + 12
    private const val FREE_RESERVE_BYTES = 64L * 1024 * 1024
    suspend fun clear(context: Context) = withContext(Dispatchers.IO) {
      val root = File(context.noBackupFilesDir, DIRECTORY)
      root.listFiles().orEmpty().forEach { group ->
        check(group.isDirectory && group.name.matches(Regex("[A-Za-z0-9_-]{43}")))
        group.listFiles().orEmpty().forEach { file ->
          check(file.isFile && file.name.matches(Regex("[A-Za-z0-9_-]{43}\\.image(\\.new|\\.bak)?")))
          check(file.delete() || !file.exists())
        }
        check(group.delete() || !group.exists())
      }
      check(!root.exists() || root.delete())
    }
  }
}
