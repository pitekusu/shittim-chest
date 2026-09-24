package dev.pitekusu.shittim.records

import android.content.Intent
import android.net.Uri
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.lifecycle.ViewModelProvider
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordAppLinkTest {
  @Test fun consumedLaunchLinkIsNotReplayedAfterRecreation() {
    val first = "/records/${"a".repeat(43)}"
    val second = "/records/${"b".repeat(43)}"
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse("https://shittim.pitekusu.dev$first"))
      .setClass(ApplicationProvider.getApplicationContext(), MainActivity::class.java)
    ActivityScenario.launch<MainActivity>(intent).use { scenario ->
      scenario.onActivity {
        val session = ViewModelProvider(it)[MobileSessionModel::class.java]
        assertEquals(first, session.loginDestination)
        session.openDestination(second)
      }
      scenario.recreate()
      scenario.onActivity {
        assertEquals(second, ViewModelProvider(it)[MobileSessionModel::class.java].loginDestination)
      }
    }
  }

  @Test fun acceptsOnlyCanonicalRecordLinks() {
    val id = "a".repeat(43)
    val path = "/records/$id"
    assertEquals(path, recordDestination("https://shittim.pitekusu.dev$path"))
    for (url in listOf(
      null,
      "https://shittim.pitekusu.dev/records/short",
      "https://shittim.pitekusu.dev$path?next=/admin",
      "https://shittim.pitekusu.dev$path#fragment",
      "https://shittim.pitekusu.dev$path%",
      "https://shittim.pitekusu.dev/records/%61${"a".repeat(42)}",
      "https://shittim.pitekusu.dev.evil.example$path",
      "http://shittim.pitekusu.dev$path",
      "https://shittim.pitekusu.dev:443$path",
    )) assertNull(recordDestination(url))
  }
}
