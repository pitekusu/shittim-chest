package dev.pitekusu.shittim.records

import android.graphics.BitmapFactory
import java.net.URI
import java.time.Instant
import java.time.OffsetDateTime
import kotlinx.serialization.Serializable

@Serializable
internal class GalleryThumbnailTicket(val cardId: String, val url: String, val expiresAt: String)

@Serializable
internal class GalleryThumbnailsResponse(val schemaVersion: Int, val items: List<GalleryThumbnailTicket>)

@Serializable
internal class GalleryOriginalTicket(val schemaVersion: Int, val url: String, val expiresAt: String)

private val galleryMediaHost =
  Regex("shittim-chest-production-records-media-[0-9]{12}\\.s3\\.[a-z0-9-]+\\.amazonaws\\.com")
private val galleryThumbnailSha = Regex("[a-f0-9]{64}\\.webp")

internal enum class GalleryImageFormat(
  val contentType: String,
  val width: Int,
  val height: Int,
  val maximum: Int = 8 * 1024 * 1024,
) {
  WEBP("image/webp", 768, 355, 512 * 1024),
  PNG("image/png", 1792, 828),
  GIF("image/gif", 1024, 473),
  APNG("image/apng", 1024, 473);

  fun validate(bytes: ByteArray) {
    check(bytes.size in 1..maximum)
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
    val decodedType = if (this == APNG) PNG.contentType else contentType
    check(bounds.outMimeType == decodedType && bounds.outWidth == width && bounds.outHeight == height)
  }
}

internal fun galleryImageFormat(url: String, cardId: String, thumbnail: Boolean): GalleryImageFormat {
  val drawId = requireNotNull(galleryDrawId(cardId))
  val uri = URI(url)
  check(url.length in 1..4096 && uri.scheme == "https" && uri.port in listOf(-1, 443) &&
    uri.userInfo == null && uri.fragment == null && galleryMediaHost.matches(uri.host.orEmpty()))
  val path = uri.rawPath.orEmpty()
  if (thumbnail) {
    val prefix = "/student-gacha/thumbnails/v1/$drawId/"
    check(path.startsWith(prefix) && galleryThumbnailSha.matches(path.removePrefix(prefix)))
    return GalleryImageFormat.WEBP
  }
  return when (path) {
    "/student-gacha/results/$drawId.png" -> GalleryImageFormat.PNG
    "/student-gacha/results/$drawId.gif" -> GalleryImageFormat.GIF
    "/student-gacha/results/$drawId.apng" -> GalleryImageFormat.APNG
    else -> error("invalid_gallery_image_path")
  }
}

internal fun checkGalleryImageExpiry(expiresAt: String) {
  if (!OffsetDateTime.parse(expiresAt).toInstant().isAfter(Instant.now())) {
    throw GalleryReadException(GalleryReadFailure.UNAVAILABLE)
  }
}
