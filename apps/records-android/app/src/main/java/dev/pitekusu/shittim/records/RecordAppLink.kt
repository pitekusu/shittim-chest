package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.isMobileReturnTo
import java.net.URI
import java.net.URISyntaxException

/** Only a canonical record URL may become a post-login destination. */
internal fun recordDestination(url: String?): String? {
  val uri = try { URI(url ?: return null) } catch (_: URISyntaxException) { return null }
  val path = uri.rawPath ?: return null
  return path.takeIf {
    uri.scheme == "https" && uri.rawAuthority == "shittim.pitekusu.dev" &&
      uri.rawQuery == null && uri.rawFragment == null &&
      path.startsWith("/records/") && isMobileReturnTo(path)
  }
}
