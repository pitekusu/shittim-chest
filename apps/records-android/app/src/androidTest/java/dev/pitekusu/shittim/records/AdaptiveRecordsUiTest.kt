package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.input.InputMode
import androidx.compose.ui.input.InputModeManager
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.platform.LocalInputModeManager
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.FontScale
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performKeyInput
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.pressKey
import androidx.compose.ui.test.then
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import java.io.File
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AdaptiveRecordsUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private val entries = (1..12).map { index ->
    RecordListEntry(index.toString().padStart(43, 'a'), "架空の相談 $index：休日に楽しめる小さなことは？",
      "レイアウト確認用", RecordAvatar(null, "cyan"), Instant.parse("2026-09-27T00:00:00Z"), "アロナ")
  }
  private val records = RecordListState.Ready.fromSaved(entries)
  private val session = SessionState.SignedIn(
    MobileSessionUser("レイアウト確認用", MobileAvatar("placeholder", "確認用", "cyan")),
    "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/")

  private fun screen(selected: String? = null, theme: ThemeChoice = ThemeChoice.Dark,
    onEvent: (BootstrapScreen.Event) -> Unit = {}): BootstrapScreen.State =
    BootstrapScreen.State(theme, session, records = records, selectedRecordId = selected,
      record = RecordPreviewState.Ready(RecordPreview("架空の議題：休日に楽しむ散歩と読書",
        "天気と気分に合わせて、無理のない小さな楽しみを選びましょう。", "アロナ"), saved = true),
      eventSink = onEvent)

  @Test fun resizeKeepsTheSelectedRecordAndListScrollAndRevocationHidesBothPanes() {
    val window = mutableStateOf(DpSize(1000.dp, 700.dp))
    val state = mutableStateOf(screen())
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(window.value)) { BootstrapUi(state.value) }
    } }
    val lastQuestion = entries.last().questionPreview
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(lastQuestion))
    compose.runOnIdle { state.value = screen(entries.last().recordId) }
    compose.onNodeWithText(lastQuestion).assertIsDisplayed()
      .assert(SemanticsMatcher.expectValue(SemanticsProperties.Selected, true))
    compose.onNodeWithTag("record-detail-content").assertIsDisplayed()
    capture("adaptive-wide-dark")
    compose.runOnIdle { window.value = DpSize(820.dp, 700.dp) }
    compose.onNodeWithTag("record-detail-content").assertDoesNotExist()
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
    compose.runOnIdle { window.value = DpSize(1000.dp, 700.dp) }
    compose.onNodeWithText(lastQuestion).assertIsDisplayed()
    compose.onNodeWithTag("record-detail-content").assertIsDisplayed()
    compose.runOnIdle { state.value = BootstrapScreen.State(ThemeChoice.Dark) {} }
    compose.onNodeWithText(lastQuestion).assertDoesNotExist()
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertDoesNotExist()
  }

  @Test fun largeTextUsesOnePaneAndTheExitRemainsVisibleWhileReading() {
    val window = mutableStateOf(DpSize(1000.dp, 700.dp))
    val state = mutableStateOf(screen(entries.first().recordId, ThemeChoice.Light))
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(window.value) then
        DeviceConfigurationOverride.FontScale(2f)) {
        BootstrapUi(screen(state.value.selectedRecordId, state.value.themeChoice, events::add))
      }
    } }
    compose.onNodeWithTag("record-detail-content").assertDoesNotExist()
    compose.onNodeWithText("一覧に戻る").assertIsDisplayed()
    compose.runOnIdle { window.value = DpSize(320.dp, 640.dp) }
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText("親愛度の変化"))
    compose.onNodeWithText("一覧に戻る").assertIsDisplayed().performClick()
    compose.runOnIdle { assertEquals(BootstrapScreen.Event.CloseRecord, events.last()) }
    capture("adaptive-large-text")
  }

  @Test fun keyboardCanOpenARecordWithoutReadingItsDecorativeAvatar() {
    val events = mutableListOf<BootstrapScreen.Event>()
    var inputMode: InputModeManager? = null
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      val manager = LocalInputModeManager.current
      SideEffect { inputMode = manager }
      BootstrapUi(screen(onEvent = events::add))
    } }
    compose.runOnIdle { requireNotNull(inputMode).requestInputMode(InputMode.Keyboard) }
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(entries.first().questionPreview))
    val card = compose.onNodeWithText(entries.first().questionPreview)
    card.performSemanticsAction(SemanticsActions.RequestFocus)
    compose.waitForIdle()
    card.assertIsFocused()
    card.performKeyInput { pressKey(Key.Enter) }
    compose.runOnIdle { assertEquals(BootstrapScreen.Event.OpenRecord(entries.first().recordId), events.last()) }
    // The fallback initial repeats the adjacent name and must not become an extra TalkBack utterance.
    compose.onNodeWithText("レ").assertDoesNotExist()
    capture("adaptive-compact-dark")
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureAdaptive") != "true") return
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
