package dev.pitekusu.shittim.records

import android.graphics.BitmapFactory
import java.net.URI
import java.security.MessageDigest
import kotlinx.serialization.Serializable

internal const val MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
internal const val MAX_ATTACHMENT_COUNT = 3
internal val ATTACHMENT_TYPES = setOf("image/jpeg", "image/png", "image/webp")
// Discord images use stable UUID5 IDs; records accept the canonical wire UUID pattern.
internal fun validRecordAttachmentId(value: String): Boolean =
  Regex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}").matches(value)

internal enum class RecordImageFailure { OFFLINE, UNAVAILABLE, INVALID, STORAGE }
internal class RecordImageException(val failure: RecordImageFailure) : Exception("record_image_${failure.name.lowercase()}")

@Serializable
internal class RecordAttachment(val attachmentId: String, val contentType: String,
  val sizeBytes: Int, val sha256: String, val width: Int, val height: Int,
  val url: String? = null, val urlExpiresAt: String? = null) {
  fun validate() {
    check(validRecordAttachmentId(attachmentId) && contentType in ATTACHMENT_TYPES)
    check(sizeBytes in 1..MAX_ATTACHMENT_BYTES && sha256.matches(Regex("[a-f0-9]{64}")))
    check(width > 0 && height > 0 && width.toLong() * height <= 40_000_000)
    check(url == null || validAttachmentS3Url(url, attachmentId))
    if (urlExpiresAt != null) check(java.time.Instant.parse(urlExpiresAt).toString().endsWith("Z"))
  }
  fun cached() = RecordAttachment(attachmentId, contentType, sizeBytes, sha256, width, height)
  fun sameImage(other: RecordAttachment): Boolean = attachmentId == other.attachmentId &&
    contentType == other.contentType && sizeBytes == other.sizeBytes && sha256 == other.sha256 &&
    width == other.width && height == other.height
  override fun toString(): String = "RecordAttachment(<redacted>)"
}

@Serializable
internal class DraftAttachment(val attachmentId: String, val contentType: String,
  val sizeBytes: Int, val sha256: String, val width: Int, val height: Int) {
  fun descriptor() = RecordAttachment(attachmentId, contentType, sizeBytes, sha256, width, height)
  fun validate() {
    // Native upload IDs remain UUID4, independently of the broader archived-image contract.
    check(validDebateRequestId(attachmentId))
    descriptor().validate()
  }
  override fun toString(): String = "DraftAttachment(<redacted>)"
}

internal class RecordImagePresentation(val image: RecordAttachment, val bytes: ByteArray? = null,
  val loading: Boolean = false, val failure: RecordImageFailure? = null) {
  override fun toString(): String = "RecordImagePresentation(<redacted>)"
}

internal fun attachmentDigest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
  .digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }

/** Decode bounds only: a 40M-pixel source must never allocate its full bitmap for validation. */
internal fun validateAttachmentBytes(bytes: ByteArray, expected: RecordAttachment? = null): Pair<Int, Int> {
  check(bytes.size in 1..MAX_ATTACHMENT_BYTES)
  val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
  BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
  check(bounds.outMimeType in ATTACHMENT_TYPES && bounds.outWidth > 0 && bounds.outHeight > 0 &&
    bounds.outWidth.toLong() * bounds.outHeight <= 40_000_000)
  expected?.let {
    it.validate()
    check(it.sizeBytes == bytes.size && it.sha256 == attachmentDigest(bytes) &&
      it.contentType == bounds.outMimeType && it.width == bounds.outWidth && it.height == bounds.outHeight)
  }
  return bounds.outWidth to bounds.outHeight
}

internal fun validAttachmentS3Url(raw: String, attachmentId: String? = null, upload: Boolean = false): Boolean = try {
  val uri = URI(raw)
  val prefix = if (upload) "shittim-chest-production-records-memorial-upload-" else "shittim-chest-production-records-media-"
  val host = Regex("${prefix}[0-9]{12}\\.s3\\.[a-z0-9-]+\\.amazonaws\\.com")
  raw.length <= 8192 && uri.scheme == "https" && uri.port in listOf(-1, 443) &&
    uri.userInfo == null && uri.fragment == null && host.matches(uri.host.orEmpty()) &&
    uri.normalize().rawPath == uri.rawPath && (if (upload) uri.rawPath in listOf("", "/")
    else attachmentId != null && uri.rawPath.matches(Regex("/debate-images/$attachmentId/original\\.(jpg|png|webp)")))
} catch (_: Exception) { false }
