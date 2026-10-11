package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import java.io.ByteArrayOutputStream
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordAttachmentsTest {
  private val id = "11111111-2222-4333-8444-abcdefabcdef"
  @Test fun archivedDiscordUuid5IsCanonicalButCannotBecomeANativeUploadId() {
    val discordId = "11111111-2222-5333-8444-abcdefabcdef"
    assertTrue(validRecordAttachmentId(discordId))
    assertFalse(validDebateRequestId(discordId))
    assertFalse(validRecordAttachmentId(discordId.uppercase()))
    assertFalse(validRecordAttachmentId(discordId.replace("-8444-", "-7444-")))
    RecordAttachment(discordId, "image/png", 1, "a".repeat(64), 1, 1).validate()
    try { DraftAttachment(discordId, "image/png", 1, "a".repeat(64), 1, 1).validate(); fail("native upload ID must be UUID4") }
    catch (_: IllegalStateException) { }
  }

  @Test fun onlyOwnedImagePathsAreAcceptedWithoutCredentialRedirects() {
    val root = "https://shittim-chest-production-records-media-000000000000.s3.ap-northeast-1.amazonaws.com"
    assertTrue(validAttachmentS3Url("$root/debate-images/$id/original.png?signature=synthetic", id))
    assertFalse(validAttachmentS3Url("$root/requesters/other.png", id))
    assertFalse(validAttachmentS3Url("https://example.invalid/debate-images/$id/original.png", id))
    assertFalse(validAttachmentS3Url("$root/debate-images/$id/../original.png", id))
    assertFalse(validAttachmentS3Url("$root:8443/debate-images/$id/original.png", id))
  }
  @Test fun actualImageBytesMustMatchMimeDimensionsSizeAndDigest() {
    val bitmap = Bitmap.createBitmap(24, 16, Bitmap.Config.ARGB_8888)
    val bytes = ByteArrayOutputStream().use { output -> bitmap.compress(Bitmap.CompressFormat.PNG, 100, output); output.toByteArray() }
    bitmap.recycle()
    val image = RecordAttachment(id, "image/png", bytes.size, attachmentDigest(bytes), 24, 16)
    assertEquals(24 to 16, validateAttachmentBytes(bytes, image))
    try { validateAttachmentBytes(bytes, RecordAttachment(id, "image/jpeg", bytes.size, image.sha256, 24, 16)); fail("mime mismatch") }
    catch (_: IllegalStateException) { }
    try { validateAttachmentBytes(bytes, RecordAttachment(id, "image/png", bytes.size, "a".repeat(64), 24, 16)); fail("digest mismatch") }
    catch (_: IllegalStateException) { }
    try { RecordAttachment(id, "image/png", 1, "a".repeat(64), 10_000, 10_000).validate(); fail("pixel limit") }
    catch (_: IllegalStateException) { }
  }
}
