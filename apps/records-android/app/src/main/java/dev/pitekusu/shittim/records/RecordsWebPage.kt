package dev.pitekusu.shittim.records

import androidx.annotation.StringRes
import dev.pitekusu.shittim.records.auth.RECORDS_ORIGIN

/** Fixed browser destinations; web login and administrator access remain web responsibilities. */
internal enum class RecordsWebPage(@StringRes val title: Int, private val path: String,
  val administrator: Boolean = false) {
  Insights(R.string.menu_insights, "/insights"),
  Momotalk(R.string.menu_momotalk, "/momotalk"),
  Memorial(R.string.menu_memorial, "/memorial"),
  Status(R.string.menu_status, "/admin", administrator = true),
  Prompts(R.string.menu_prompts, "/admin/prompts", administrator = true);

  val url: String get() = "$RECORDS_ORIGIN$path"
}
