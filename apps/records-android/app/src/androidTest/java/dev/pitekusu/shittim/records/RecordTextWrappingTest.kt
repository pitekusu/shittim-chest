package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import android.icu.text.BreakIterator
import android.os.Build
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.LineBreak
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordTextWrappingTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  private val japanese = "休日には友だちと図書館で読書を楽しみます。仕事の合間には東京都の公園を散歩します。" +
    "「好きな本」を持って、ゆっくり過ごしましょう。"
  private val mixed = "英数字ABC123XYZと絵文字😊、家族👨‍👩‍👧‍👦の話です。\n" +
    "次の行では「音楽」と日本語の読み方を確かめます。"

  @Test fun JapaneseBodyKeepsWordsPunctuationExplicitNewlinesAndEmojiWithoutChangingTheText() {
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      Column(Modifier.width(240.dp).verticalScroll(rememberScrollState())) {
        Text(japanese, style = MaterialTheme.typography.bodyLarge)
        Text(mixed, style = MaterialTheme.typography.bodyLarge)
      }
    } } }
    val paragraph = layout(japanese)
    assertTrue(paragraph.lineCount > 1)
    assertJapaneseLayout(paragraph, japanese)
    if (Build.VERSION.SDK_INT >= 33) {
      // Check representative short words, not a device/dictionary-specific complete line list.
      assertWordsAreNotSplit(paragraph, listOf("友だち", "図書館", "東京都"))
    }
    val mixedLayout = layout(mixed)
    assertJapaneseLayout(mixedLayout, mixed)
    val newline = mixed.indexOf('\n')
    assertTrue(mixedLayout.getLineForOffset(newline + 1) > mixedLayout.getLineForOffset(newline - 1))
  }

  @Test fun MarkdownInheritsJapaneseWrappingAndKeepsLinkDestinationsAndCodeLayout() {
    val heading = "図書館で楽しむ読書と、東京都の公園をめぐる休日の過ごし方"
    val bullet = "図書館では静かに読書を楽しみ、東京都の公園では「好きな音楽」を聴きましょう。"
    val label = "日本語の参考リンク"
    val url = "https://example.com/docs?q=wrap%20sample#part"
    val token = "map<String, Int>"
    val inline = "式は $token です。"
    val code = "val result = listOf(\"A\", \"B\")\nprintln(result.joinToString(\"-\"))"
    val source = "# $heading\n\n$japanese\n\n- $bullet\n\n[$label]($url)\n\n" +
      "式は`$token`です。\n\n```kotlin\n$code\n```"
    val opened = mutableListOf<String>()
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(true) {
      CompositionLocalProvider(LocalUriHandler provides object : UriHandler {
        override fun openUri(uri: String) { opened += uri }
      }) {
        Column(Modifier.width(248.dp).verticalScroll(rememberScrollState())) { RecordMarkdown(source) }
      }
    } } }
    compose.waitUntil(10_000) {
      listOf(heading, japanese, bullet, code).all {
        compose.onAllNodesWithText(it).fetchSemanticsNodes().isNotEmpty()
      }
    }
    for (text in listOf(heading, japanese, bullet)) assertJapaneseLayout(layout(text), text)
    val link = layout(label).layoutInput.text
    assertEquals(label, link.text)
    assertEquals(url, (link.getLinkAnnotations(0, link.length).single().item as LinkAnnotation.Url).url)
    compose.onAllNodesWithText(label, substring = true).onFirst().performScrollTo().performClick()
    compose.runOnIdle { assertEquals(listOf(url), opened) }
    // Inline code uses a SpanStyle, so paragraph breaking remains Japanese; no source rewrite.
    val inlineLayout = layout(inline)
    assertEquals(inline, inlineLayout.layoutInput.text.text)
    assertTrue(inlineLayout.layoutInput.text.spanStyles.any {
      it.item.fontFamily == FontFamily.Monospace &&
        inline.substring(it.start, it.end).contains(token)
    })
    val codeLayout = layout(code)
    assertEquals(code, codeLayout.layoutInput.text.text)
    assertEquals(FontFamily.Monospace, codeLayout.layoutInput.style.fontFamily)
    assertEquals(LineBreak.Simple, codeLayout.layoutInput.style.lineBreak)
    assertEquals(2, codeLayout.lineCount) // Existing horizontal scrolling must not add soft line breaks.
  }

  @Test fun fakeDetailPreviewRemainsReadableInBothThemesAndAt320dpWithDoubleText() {
    val dark = mutableStateOf(false)
    val large = mutableStateOf(false)
    // The renderer's normal Markdown soft-line-break rule is unchanged by wrapping styles.
    val mixedParagraph = mixed.replace('\n', ' ')
    val body = "$japanese\n\n$mixedParagraph"
    val preview = RecordPreview("図書館で読書を楽しむ休日を、どのように過ごしますか？", body, "プラナ",
      opinions = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
        RecordOpinion(name, "読みやすい日本語で提案します", "## 自然な日本語の折り返し\n\n$body",
          "一緒に楽しめる休日", body, "participant-${('a'.code + index).toChar()}")
      })
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(dark.value) {
      val density = LocalDensity.current
      CompositionLocalProvider(LocalDensity provides Density(density.density, if (large.value) 2f else 1f)) {
        ShittimBackdrop {
          Box(Modifier.width(if (large.value) 320.dp else 420.dp).fillMaxHeight()
            .windowInsetsPadding(WindowInsets.safeDrawing)) {
            RecordDetailScreen(RecordPreviewState.Ready(preview), "wrapping-preview", {}, motionAllowed = false)
          }
        }
      }
    } } }
    for (mode in listOf(false, true)) {
      for (doubleText in listOf(false, true)) {
        compose.runOnIdle { dark.value = mode; large.value = doubleText }
        compose.waitUntil(10_000) { compose.onAllNodesWithText(japanese).fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText(japanese).performScrollTo()
        compose.waitForIdle()
        assertJapaneseLayout(layout(japanese), japanese)
        assertJapaneseLayout(layout(mixedParagraph), mixedParagraph)
        capture("japanese-wrap-${if (mode) "dark" else "light"}-${if (doubleText) "320dp-2x" else "normal"}")
      }
    }
  }

  private fun layout(text: String): TextLayoutResult {
    val results = mutableListOf<TextLayoutResult>()
    compose.onNode(hasText(text) and SemanticsMatcher.keyIsDefined(SemanticsActions.GetTextLayoutResult),
      useUnmergedTree = true).performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(results) }
    return results.single()
  }

  private fun assertJapaneseLayout(result: TextLayoutResult, text: String) {
    assertEquals(text, result.layoutInput.text.text)
    assertEquals(LineBreak.WordBreak.Phrase, result.layoutInput.style.lineBreak.wordBreak)
    assertEquals("ja", result.layoutInput.style.localeList?.get(0)?.language)
    assertFalse(result.hasVisualOverflow)
    val characters = BreakIterator.getCharacterInstance(Locale.JAPAN).apply { setText(text) }
    for (line in 0 until result.lineCount) {
      assertFalse(result.isLineEllipsized(line))
      val start = result.getLineStart(line)
      val end = result.getLineEnd(line)
      assertTrue("A line must not split an emoji/grapheme", characters.isBoundary(start))
      assertTrue("A line must not split an emoji/grapheme", characters.isBoundary(end))
      val visible = text.substring(start, end).trim()
      if (visible.isNotEmpty()) {
        assertFalse("Closing punctuation must not start a line: $visible", visible.first() in "、。）」』】！？")
        assertFalse("Opening punctuation must not end a line: $visible", visible.last() in "（「『【")
      }
    }
  }

  private fun assertWordsAreNotSplit(result: TextLayoutResult, words: List<String>) {
    val text = result.layoutInput.text.text
    for (word in words) {
      val start = text.indexOf(word)
      assertTrue(start >= 0)
      for (line in 0 until result.lineCount - 1) {
        assertFalse("A representative Japanese word must stay together: $word",
          result.getLineEnd(line) in (start + 1) until (start + word.length))
      }
    }
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureUi") != "true") return
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
