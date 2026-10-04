package dev.pitekusu.shittim.records

import android.content.Intent
import android.net.Uri
import androidx.test.core.app.ActivityScenario
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleCallback
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import androidx.lifecycle.ViewModelProvider
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.auth.PendingDestination
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordAppLinkTest {
  @Test fun coldLinkIsQueuedOnceAndRecreationDoesNotReplaceThePendingRequest() {
    val destination = "/records/${"a".repeat(43)}"
    ActivityScenario.launch<MainActivity>(link(destination)).use { scenario ->
      var pending: PendingDestination? = null
      scenario.onActivity {
        val session = ViewModelProvider(it)[MobileSessionModel::class.java]
        pending = session.pendingDestination.value
        assertEquals(destination, pending?.returnTo)
        assertNull(session.offlineCacheAccountId) // A route is not permission to read a record.
      }
      scenario.recreate()
      scenario.onActivity {
        assertSame(pending, ViewModelProvider(it)[MobileSessionModel::class.java].pendingDestination.value)
      }
    }
  }

  @Test fun warmLinkUsesTheSameValidationAndDiscardedLinksCannotReappearAfterRecreation() {
    val first = "/records/${"a".repeat(43)}"
    val second = "/records/${"b".repeat(43)}"
    ActivityScenario.launch<MainActivity>(link(first)).use { scenario ->
      var pending: PendingDestination? = null
      deliver(scenario, link(second))
      scenario.onActivity {
        val session = ViewModelProvider(it)[MobileSessionModel::class.java]
        pending = session.pendingDestination.value
        assertEquals(second, pending?.returnTo)
      }
      deliver(scenario, link("$first?next=/admin"))
      scenario.onActivity {
        val session = ViewModelProvider(it)[MobileSessionModel::class.java]
        assertSame(pending, session.pendingDestination.value)
        session.discardDestination()
      }
      scenario.recreate()
      scenario.onActivity {
        assertNull(ViewModelProvider(it)[MobileSessionModel::class.java].pendingDestination.value)
      }
    }
  }

  @Test fun staleNotificationCannotQueueAColdLinkOrReplaceAWarmDestination() {
    val destination = "/records/${"a".repeat(43)}"
    val stale = link(destination).putExtra(RECORD_NOTIFICATION_BINDING, "stale-binding")
    ActivityScenario.launch<MainActivity>(stale).use { scenario ->
      var pending: PendingDestination? = null
      scenario.onActivity {
        assertNull(ViewModelProvider(it)[MobileSessionModel::class.java].pendingDestination.value)
      }
      deliver(scenario, link(destination))
      scenario.onActivity {
        pending = ViewModelProvider(it)[MobileSessionModel::class.java].pendingDestination.value
        assertEquals(destination, pending?.returnTo)
      }
      deliver(scenario, stale)
      scenario.onActivity {
        val session = ViewModelProvider(it)[MobileSessionModel::class.java]
        assertSame(pending, session.pendingDestination.value)
      }
    }
  }

  private fun link(path: String) = Intent(Intent.ACTION_VIEW, Uri.parse("https://shittim.pitekusu.dev$path"))
    .setClass(ApplicationProvider.getApplicationContext(), MainActivity::class.java)

  private fun deliver(scenario: ActivityScenario<MainActivity>, intent: Intent) {
    val instrumentation = InstrumentationRegistry.getInstrumentation()
    val lifecycle = ActivityLifecycleMonitorRegistry.getInstance()
    val resumed = CountDownLatch(1)
    lateinit var target: MainActivity
    scenario.onActivity { target = it }
    val callback = ActivityLifecycleCallback { activity, stage ->
      if (activity === target && stage == Stage.RESUMED) resumed.countDown()
    }
    lifecycle.addLifecycleCallback(callback)
    try {
      // onNewIntent is followed by onResume; main-thread idle alone can precede Binder delivery.
      scenario.onActivity { it.startActivity(Intent(intent).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)) }
      assertTrue("warm link did not resume its Activity", resumed.await(5, TimeUnit.SECONDS))
      instrumentation.waitForIdleSync()
    } finally {
      lifecycle.removeLifecycleCallback(callback)
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
