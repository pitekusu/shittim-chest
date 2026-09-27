package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.material3.ColorScheme
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.core.graphics.ColorUtils
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ShittimDesignTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun backdropProvidesReadableTextAndTheThemeUsesBundledTypeInBothModes() {
    val dark = mutableStateOf(false)
    var colors: ColorScheme? = null
    var inherited = Color.Unspecified
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(dark.value) { ShittimBackdrop {
        val scheme = MaterialTheme.colorScheme
        val contentColor = LocalContentColor.current
        val type = MaterialTheme.typography
        SideEffect {
          colors = scheme
          inherited = contentColor
          assertNotNull(type.bodyLarge.fontFamily)
          assertEquals(type.bodyLarge.fontFamily, type.titleLargeEmphasized.fontFamily)
          assertEquals(type.bodyLarge.fontFamily, type.labelLarge.fontFamily)
        }
        // No explicit color: this used to fall back to black even in dark mode.
        Text("背景上の読み込みメッセージ")
      } } }
    }
    for (mode in listOf(false, true)) {
      compose.runOnIdle { dark.value = mode }
      compose.runOnIdle {
        val scheme = requireNotNull(colors)
        assertEquals(scheme.onBackground, inherited)
        for (surface in listOf(scheme.background, scheme.primaryContainer,
          scheme.surfaceContainerLow, scheme.surfaceContainerHigh)) {
          for (ink in listOf(scheme.onSurface, scheme.onSurfaceVariant,
            scheme.primary, scheme.secondary, scheme.tertiary)) {
            assertTrue("Unreadable text pair in dark=$mode",
              ColorUtils.calculateContrast(ink.toArgb(), surface.toArgb()) >= 4.5)
          }
        }
      }
    }
  }

  @Test fun sharedComponentsRemainOperableInLightDarkAndLargeTextScreens() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val user = MobileSessionUser("デザイン確認用", MobileAvatar("placeholder", "確認用", "cyan"))
    val session = SessionState.SignedIn(user, "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/")
    val entries = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
      RecordListEntry(('a' + index).toString().repeat(43), "架空の相談：休日をどう過ごそう？",
        "デザイン確認用", RecordAvatar(null, listOf("cyan", "pink", "lavender")[index]),
        Instant.parse("2026-09-27T00:00:00Z"), name)
    }
    val state = mutableStateOf(BootstrapScreen.State(ThemeChoice.Light, session,
      records = RecordListState.Ready.fromSaved(entries), sync = RecordSyncState.Running,
      eventSink = events::add))
    val large = mutableStateOf(false)
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        val density = LocalDensity.current
        CompositionLocalProvider(LocalDensity provides Density(density.density, if (large.value) 2f else 1f)) {
          Box(Modifier.width(if (large.value) 320.dp else 360.dp)) { BootstrapUi(state.value) }
        }
      }
    }
    val list = compose.onNodeWithTag("bootstrap-content")
    list.performScrollToNode(hasText("勝者：プラナ"))
    capture("design-records-light")
    compose.runOnIdle {
      state.value = BootstrapScreen.State(ThemeChoice.Dark, session,
        records = state.value.records, sync = RecordSyncState.Running, eventSink = events::add)
    }
    capture("design-records-dark")
    list.performScrollToNode(hasText("勝者：プラナ"))
    compose.onNodeWithText("勝者：プラナ").assertIsDisplayed()
    compose.runOnIdle { large.value = true }
    list.performScrollToNode(hasTestTag("winner-Abe"))
    compose.onNodeWithTag("winner-Abe").assertIsDisplayed().performClick()
    compose.runOnIdle { assertEquals(BootstrapScreen.Event.SelectWinner(RecordWinner.Abe), events.last()) }
    capture("design-large-text")
    list.performScrollToNode(hasText(compose.activity.getString(R.string.session_logout)))
    compose.onNodeWithText(compose.activity.getString(R.string.session_logout)).performClick()
    compose.runOnIdle { assertEquals(BootstrapScreen.Event.Logout, events.last()) }

    val opinions = listOf("アロナ", "プラナ", "安倍晋三AI").map { name ->
      RecordOpinion(name, "休日の過ごし方", "好きなことを一つ楽しみませんか。",
        "小さな楽しみを選ぶ", "**散歩や読書**で、無理のない休日にしましょう。")
    }
    compose.runOnIdle {
      large.value = false
      state.value = BootstrapScreen.State(ThemeChoice.Dark, session,
        records = state.value.records, selectedRecordId = entries.first().recordId,
        record = RecordPreviewState.Ready(RecordPreview("架空の相談：休日をどう過ごそう？",
          "気分に合わせて、小さな楽しみを選びましょう。", "アロナ", opinions)), eventSink = events::add)
    }
    list.performScrollToNode(hasText("プラナ"))
    compose.onNodeWithText("プラナ").assertIsDisplayed()
    capture("design-detail-dark")
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureDesign") != "true") return
    val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
