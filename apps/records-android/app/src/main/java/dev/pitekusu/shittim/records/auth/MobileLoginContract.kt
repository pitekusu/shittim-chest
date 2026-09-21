package dev.pitekusu.shittim.records.auth

import android.app.Activity
import android.content.Context
import android.content.Intent
import androidx.activity.result.contract.ActivityResultContract

internal const val LOGIN_RETURN_TO = "returnTo"
internal const val LOGIN_STATUS = "status"

/** The caller receives a status/destination only. Credentials never cross Activity results. */
internal class MobileLoginContract : ActivityResultContract<String, MobileLoginStep.Finished>() {
  override fun createIntent(context: Context, input: String): Intent {
    require(isMobileReturnTo(input)) { "invalid_mobile_destination" }
    return Intent(context, MobileLoginActivity::class.java).putExtra(LOGIN_RETURN_TO, input)
  }

  override fun parseResult(resultCode: Int, intent: Intent?): MobileLoginStep.Finished {
    if (resultCode != Activity.RESULT_OK) return MobileLoginStep.Finished(MobileLoginStatus.CANCELLED)
    val status = MobileLoginStatus.entries.find { it.name == intent?.getStringExtra(LOGIN_STATUS) }
      ?: return MobileLoginStep.Finished(MobileLoginStatus.REJECTED)
    val returnTo = intent?.getStringExtra(LOGIN_RETURN_TO)
    return if (status == MobileLoginStatus.SIGNED_IN && !isMobileReturnTo(returnTo)) {
      MobileLoginStep.Finished(MobileLoginStatus.REJECTED)
    } else MobileLoginStep.Finished(status, returnTo.takeIf { status == MobileLoginStatus.SIGNED_IN })
  }
}

internal fun isMobileReturnTo(value: String?): Boolean =
  value != null && Regex("/(?:records/[A-Za-z0-9_-]{43})?").matches(value)
