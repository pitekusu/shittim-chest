package dev.pitekusu.shittim.records

import android.content.ActivityNotFoundException
import android.content.Context
import android.net.Uri
import androidx.annotation.StringRes
import androidx.browser.customtabs.CustomTabsIntent
import dev.pitekusu.shittim.records.auth.RECORDS_ORIGIN

/** Only these two anonymous pages may be opened explicitly from the native demo. */
internal enum class RecordsPublicPage(@StringRes val title: Int, private val path: String) {
  PrivacyPolicy(R.string.privacy_policy, "/privacy-policy"),
  DataDeletion(R.string.data_deletion_request, "/data-deletion");

  val url: String get() = "$RECORDS_ORIGIN$path"
}

internal fun launchRecordsPublicPage(context: Context, page: RecordsPublicPage): Boolean =
  launchRecordsBrowserPage(context, page.url)

internal fun launchRecordsWebPage(context: Context, page: RecordsWebPage): Boolean =
  launchRecordsBrowserPage(context, page.url)

private fun launchRecordsBrowserPage(context: Context, url: String): Boolean = try {
  CustomTabsIntent.Builder().setShowTitle(true).build().launchUrl(context, Uri.parse(url))
  true
} catch (_: ActivityNotFoundException) {
  false
} catch (_: SecurityException) {
  false
}
