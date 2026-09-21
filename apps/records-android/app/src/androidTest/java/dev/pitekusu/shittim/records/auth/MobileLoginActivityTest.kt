package dev.pitekusu.shittim.records.auth

import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import androidx.activity.result.ActivityResult
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.contract.ActivityResultContracts
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.MainActivity
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MobileLoginActivityTest {
  private val context = InstrumentationRegistry.getInstrumentation().targetContext
  private val callback = "$MOBILE_CALLBACK_URL?transaction=${"t".repeat(43)}&code=${"c".repeat(43)}&state=${"s".repeat(43)}"

  @Test
  fun coldCallbackFinishesWithoutStartingAuthenticationAndReturnsNoGrant() {
    val intent = Intent(context, MobileLoginActivity::class.java).setData(Uri.parse(callback))
    // The controller deliberately clears its Intent URL. Observe the caller's result rather than
    // ActivityScenario's Intent matching, which cannot track an Activity after that redaction.
    val results = ArrayBlockingQueue<ActivityResult>(1)
    var launcher: ActivityResultLauncher<Intent>? = null
    ActivityScenario.launch(MainActivity::class.java).use { scenario ->
      scenario.onActivity { activity ->
        launcher = activity.activityResultRegistry.register("cold_callback", ActivityResultContracts.StartActivityForResult()) {
          results.add(it)
        }.also { it.launch(intent) }
      }
      val result = requireNotNull(results.poll(5, TimeUnit.SECONDS))
      val resultData = requireNotNull(result.data)
      val parsed = MobileLoginContract().parseResult(result.resultCode, resultData)
      assertEquals(MobileLoginStatus.REJECTED, parsed.status)
      assertNull(resultData.data)
      assertEquals(setOf(LOGIN_STATUS, LOGIN_RETURN_TO), resultData.extras!!.keySet())
      scenario.onActivity { launcher?.unregister() }
    }
  }

  @Test
  fun onlyExactHttpsCallbackIsExportedAndContractRejectsExternalDestinations() {
    val manager = context.packageManager
    assertFalse(manager.getActivityInfo(ComponentName(context, MobileLoginActivity::class.java), 0).exported)
    assertTrue(manager.getActivityInfo(ComponentName(context, MobileLoginRedirectActivity::class.java), 0).exported)
    for ((url, expected) in listOf(
      callback to 1, callback.replace("https:", "http:") to 0,
      callback.replace("/callback?", "/callback/extra?") to 0,
      callback.replace("shittim.pitekusu.dev", "untrusted.invalid") to 0,
    )) {
      val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url)).addCategory(Intent.CATEGORY_BROWSABLE)
        .setPackage(context.packageName)
      assertEquals(expected, manager.queryIntentActivities(intent, PackageManager.MATCH_DEFAULT_ONLY).size)
    }
    val contract = MobileLoginContract()
    val intent = contract.createIntent(context, "/")
    assertEquals(setOf(LOGIN_RETURN_TO), intent.extras!!.keySet())
    assertThrows(IllegalArgumentException::class.java) { contract.createIntent(context, "https://untrusted.invalid") }
    val forged = Intent().putExtra(LOGIN_STATUS, "SIGNED_IN").putExtra(LOGIN_RETURN_TO, "//untrusted.invalid")
    assertEquals(MobileLoginStatus.REJECTED, contract.parseResult(Activity.RESULT_OK, forged).status)
    assertEquals(MobileLoginStatus.CANCELLED, contract.parseResult(Activity.RESULT_CANCELED, null).status)
  }
}
