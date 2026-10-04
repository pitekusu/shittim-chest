package dev.pitekusu.shittim.records

import android.view.ViewGroup
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.navigation3.runtime.NavBackStack
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.rememberNavBackStack
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordRoutesTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun restoredDetailKeepsItsListParentAndClosingDoesNotRetainDetailHistory() {
    val restoration = StateRestorationTester(compose)
    compose.activityRule.scenario.onActivity { activity ->
      activity.findViewById<ViewGroup>(android.R.id.content).removeAllViews()
    }
    lateinit var stack: NavBackStack<NavKey>
    restoration.setContent { stack = rememberNavBackStack(RecordsList) }
    val first = "a".repeat(43)
    val second = "b".repeat(43)
    compose.runOnIdle {
      stack.openRecord(first)
      stack.openRecord(first) // Repeated selection doesn't add history.
      assertEquals(listOf(RecordsList, RecordDetail(first)), stack.toList())
    }
    restoration.emulateSavedInstanceStateRestore()
    compose.runOnIdle {
      assertEquals(listOf(RecordsList, RecordDetail(first)), stack.toList())
      stack.openRecord(second) // An incoming link replaces the current detail.
      assertEquals(listOf(RecordsList, RecordDetail(second)), stack.toList())
      stack.closeRecord()
      stack.closeRecord() // Root can never be popped.
      assertEquals(listOf(RecordsList), stack.toList())
      stack.openRecord(first)
      assertEquals(listOf(RecordsList, RecordDetail(first)), stack.toList())
    }
  }

  @Test fun routePayloadContainsOnlyOpaqueIdAndRejectsNoncanonicalIds() {
    val id = "a".repeat(43)
    val payload = Json.encodeToString(RecordDetail(id))
    assertEquals("""{"recordId":"$id"}""", payload)
    assertEquals(RecordDetail(id), Json.decodeFromString<RecordDetail>(payload))
    for (invalid in listOf("short", "/records/$id", "$id?next=/admin", "x".repeat(44))) {
      try { RecordDetail(invalid); fail("noncanonical route") }
      catch (_: IllegalArgumentException) { }
    }
  }
}
