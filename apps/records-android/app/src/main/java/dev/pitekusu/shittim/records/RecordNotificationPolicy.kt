package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.StoredToken
import java.security.MessageDigest
import java.text.Normalizer
import java.time.Duration
import java.time.Instant

private val notificationOpaque = Regex("[A-Za-z0-9_-]{43}")

/** Only a publication hint and display name; never a record or an authorization grant. */
internal class RecordPublishedHint(val recordId: String, val bindingId: String, val publishedAt: Instant,
  val requesterName: String) {
  companion object {
    fun parse(data: Map<String, String>): RecordPublishedHint? {
      if (data.keys != setOf("type", "schemaVersion", "recordId", "bindingId", "publishedAt", "requesterName") ||
        data["type"] != "record_published" || data["schemaVersion"] != "1") return null
      val record = data["recordId"]?.takeIf(notificationOpaque::matches) ?: return null
      val binding = data["bindingId"]?.takeIf(notificationOpaque::matches) ?: return null
      val date = data["publishedAt"]?.takeIf { it.length <= 32 && it.endsWith("Z") } ?: return null
      val published = try { Instant.parse(date) } catch (_: Exception) { return null }
      val name = data["requesterName"]?.takeIf {
        it.codePointCount(0, it.length) in 1..100 && it == it.trim() &&
          Normalizer.isNormalized(it, Normalizer.Form.NFC) &&
          it.codePoints().noneMatch { point -> Character.isISOControl(point) ||
            point in 0xD800..0xDFFF || point == 0x2028 || point == 0x2029 ||
            (Character.getType(point) == Character.FORMAT.toInt() && point != 0x200C && point != 0x200D) }
      } ?: return null
      return RecordPublishedHint(record, binding, published, name)
    }
  }
}

internal fun notificationSessionFingerprint(token: String): String =
  MessageDigest.getInstance("SHA-256").digest(token.toByteArray(Charsets.US_ASCII))
    .joinToString("") { "%02x".format(it) }

internal fun notificationAllowed(hint: RecordPublishedHint, stored: StoredToken?, optedIn: Boolean,
  permission: Boolean, binding: String?, sessionFingerprint: String?, registeredAt: Instant?,
  registrationExpiresAt: Instant?, logoutPending: Boolean, now: Instant): Boolean {
  val permit = stored?.cacheAuthorization ?: return false
  return optedIn && permission && !logoutPending && stored.expiresAt > now && permit.permits(now) &&
    binding == hint.bindingId && sessionFingerprint == notificationSessionFingerprint(stored.accessToken) &&
    registeredAt != null && registrationExpiresAt != null && registrationExpiresAt > now &&
    // Ignore queued events predating registration and stale/future payloads.
    hint.publishedAt >= registeredAt && hint.publishedAt <= now.plusSeconds(60) &&
    hint.publishedAt >= now.minus(Duration.ofHours(24))
}
