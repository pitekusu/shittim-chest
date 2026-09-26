package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.ByteArrayOutputStream
import java.net.URI
import java.security.MessageDigest
import java.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

internal fun validStoredAvatarUrl(url: String): Boolean = try {
  val uri = URI(url)
  url.length <= 4096 && uri.scheme == "https" && uri.port in listOf(-1, 443) &&
    uri.userInfo == null && uri.fragment == null &&
    Regex("[a-z0-9.-]+\\.s3(?:\\.ap-northeast-1)?\\.amazonaws\\.com").matches(uri.host.orEmpty()) &&
    uri.rawPath.startsWith("/requesters/") && uri.normalize().rawPath == uri.rawPath
} catch (_: Exception) { false }

// Ignore signature/expiry parameters; an unchanged icon is never downloaded again.
internal fun avatarSource(url: String): String = URI(url).let { uri ->
  Base64.getUrlEncoder().withoutPadding().encodeToString(MessageDigest.getInstance("SHA-256")
    .digest("${uri.host}${uri.rawPath}".toByteArray(Charsets.UTF_8)))
}

internal suspend fun thumbnailAvatar(bytes: ByteArray): ByteArray = withContext(Dispatchers.Default) {
  try {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
    check(bounds.outWidth in 1..4096 && bounds.outHeight in 1..4096)
    val options = BitmapFactory.Options().apply {
      inSampleSize = Integer.highestOneBit((maxOf(bounds.outWidth, bounds.outHeight) / 128).coerceAtLeast(1))
    }
    val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) ?: error("invalid_avatar")
    try {
      val scale = 128f / maxOf(bitmap.width, bitmap.height)
      val resized = Bitmap.createScaledBitmap(bitmap, (bitmap.width * scale).toInt().coerceAtLeast(1),
        (bitmap.height * scale).toInt().coerceAtLeast(1), true)
      try {
        ByteArrayOutputStream().use { output ->
          check(resized.compress(Bitmap.CompressFormat.PNG, 100, output))
          output.toByteArray().also { check(it.size in 1..131_072) }
        }
      } finally { if (resized !== bitmap) resized.recycle() }
    } finally { bitmap.recycle() }
  } catch (_: Exception) { throw RecordReadException(RecordReadFailure.INVALID_RESPONSE) }
  finally { bytes.fill(0) }
}
