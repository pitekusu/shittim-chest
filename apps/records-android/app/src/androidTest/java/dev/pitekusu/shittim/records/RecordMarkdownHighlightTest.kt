package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.ComposeTimeoutException
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.printToString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.core.graphics.ColorUtils
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import coil3.ImageLoader
import coil3.SingletonImageLoader
import coil3.annotation.DelicateCoilApi
import coil3.compose.AsyncImage
import coil3.intercept.Interceptor
import coil3.request.CachePolicy
import coil3.request.ErrorResult
import coil3.request.ImageResult
import coil3.request.ImageRequest
import com.mikepenz.markdown.model.MarkdownState
import com.mikepenz.markdown.model.rememberMarkdownState
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.Dispatchers
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordMarkdownHighlightTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun highlightsFollowThemeChangesWithoutReplacingParsedContent() {
    val dark = mutableStateOf(false)
    val highlighted = "大切な結論と理由を、本文の流れを保ちながら読み進められます。"
    val source = "# 強調表示\n\n==$highlighted==\n\n通常の本文も読みやすく表示します。"
    var state: MarkdownState? = null
    var expected = Color.Unspecified to Color.Unspecified
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(dark.value) {
      val density = LocalDensity.current
      expected = MaterialTheme.colorScheme.onSecondaryContainer to MaterialTheme.colorScheme.secondaryContainer
      val parsed = rememberMarkdownState(source)
      state = parsed
      CompositionLocalProvider(LocalDensity provides Density(density.density, 2f)) {
        ShittimBackdrop {
          Column(Modifier.width(320.dp).fillMaxHeight().windowInsetsPadding(WindowInsets.safeDrawing)
            .verticalScroll(rememberScrollState()).padding(16.dp)) {
            RecordMarkdown(source, markdownState = parsed)
          }
        }
      }
    } } }
    waitForText(highlighted)
    val originalState = compose.runOnIdle { checkNotNull(state) }
    val renderedColors = mutableSetOf<Pair<Color, Color>>()
    for (mode in listOf(false, true)) {
      compose.runOnIdle { dark.value = mode }
      compose.waitForIdle()
      val result = layout(highlighted)
      val colors = compose.runOnIdle { expected }
      assertSame(originalState, compose.runOnIdle { state })
      assertEquals(highlighted, result.layoutInput.text.text)
      val style = result.layoutInput.text.spanStyles.firstOrNull {
        it.start == 0 && it.end == highlighted.length &&
          it.item.color == colors.first && it.item.background == colors.second
      }
      assertTrue("Rendered text must use the current theme's highlight colors", style != null)
      val actual = checkNotNull(style).item
      assertTrue(ColorUtils.calculateContrast(actual.color.toArgb(), actual.background.toArgb()) >= 4.5)
      renderedColors += actual.color to actual.background
      assertTrue(result.lineCount > 1)
      assertFalse(result.hasVisualOverflow)
      capture("markdown-highlight-${if (mode) "dark" else "light"}-320dp-2x")
    }
    assertEquals("Light and dark must render different highlight colors", 2, renderedColors.size)
  }

  @Test fun nestedFormattingAndLiteralEqualsRemainReadable() {
    val nested = "大切な 結論 と 理由 を読む"
    val literal = "等号 a = b と a == b と ==未強調== を残す。"
    val code = "==block==\na == b"
    val source = "==大切な **結論** と *理由* を読む==\n\n" +
      "等号 a = b と a == b と \\=\\=未強調\\=\\= を残す。\n\n" +
      "インライン `==code==` と `a == b`。\n\n```text\n$code\n```"
    var highlightBackground = Color.Unspecified
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      highlightBackground = MaterialTheme.colorScheme.secondaryContainer
      Column(Modifier.width(320.dp).verticalScroll(rememberScrollState())) { RecordMarkdown(source) }
    } } }
    for (text in listOf(nested, literal, code)) waitForText(text)
    waitForText("インライン", substring = true)
    val formatted = layout(nested).layoutInput.text
    assertEquals(nested, formatted.text)
    assertTrue(formatted.spanStyles.any {
      it.item.fontWeight == FontWeight.Bold && nested.substring(it.start, it.end) == "結論"
    })
    assertTrue(formatted.spanStyles.any {
      it.item.fontStyle == FontStyle.Italic && nested.substring(it.start, it.end) == "理由"
    })
    for (text in listOf(literal, code)) {
      val result = layout(text)
      assertEquals(text, result.layoutInput.text.text)
      assertFalse(result.layoutInput.text.spanStyles.any { it.item.background == highlightBackground })
    }
    val inline = layout("インライン", substring = true).layoutInput.text
    assertFalse(inline.spanStyles.any { it.item.background == highlightBackground })
    for (expectedCode in listOf("==code==", "a == b")) {
      assertTrue(inline.spanStyles.any {
        // The renderer pads inline code; compare the code contents without that presentation spacing.
        it.item.fontFamily == FontFamily.Monospace &&
          inline.text.substring(it.start, it.end).trim() == expectedCode
      })
    }
    assertEquals(FontFamily.Monospace, layout(code).layoutInput.style.fontFamily)
  }

  @OptIn(DelicateCoilApi::class)
  @Test fun highlightedLinksKeepUrlPolicyAndMarkdownImagesDoNotRequestALoader() {
    val url = "https://example.com/reference?q=highlight#part"
    val imageUrl = "https://example.com/markdown-highlight-image.webp"
    val source = "==[参考ページ]($url)==\n\n==[拒否リンク](intent://other.app)==\n\n" +
      "==![画像の代替文]($imageUrl)==\n\n表示の最後"
    val opened = mutableListOf<String>()
    val imageRequests = AtomicInteger()
    val controlRequests = AtomicInteger()
    val controlErrors = AtomicInteger()
    val showControl = mutableStateOf(false)
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    val controlUrl = "https://example.com/markdown-image-control.webp"
    val controlRequest = ImageRequest.Builder(context).data(controlUrl).size(1).build()
    val previousLoader = SingletonImageLoader.get(context)
    val recordingLoader = ImageLoader.Builder(context)
      .memoryCachePolicy(CachePolicy.DISABLED).diskCachePolicy(CachePolicy.DISABLED)
      // Keep the non-suspending sentinel on the same main dispatcher as Compose image requests.
      .interceptorCoroutineContext(Dispatchers.Main.immediate)
      .components {
        add(object : Interceptor {
          override suspend fun intercept(chain: Interceptor.Chain): ImageResult {
            if (chain.request.data.toString() == imageUrl) imageRequests.incrementAndGet()
            if (chain.request.data.toString() == controlUrl) controlRequests.incrementAndGet()
            // Stop before any network or disk access even if Markdown starts requesting images.
            return ErrorResult(null, chain.request, IllegalStateException("markdown_image_request_forbidden"))
          }
        })
      }.build()
    try {
      SingletonImageLoader.setUnsafe(recordingLoader)
      compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
        CompositionLocalProvider(LocalUriHandler provides object : UriHandler {
          override fun openUri(uri: String) { opened += uri }
        }) {
          Column(Modifier.width(320.dp).verticalScroll(rememberScrollState())) {
            RecordMarkdown(source)
            if (showControl.value) AsyncImage(model = controlRequest, imageLoader = recordingLoader,
              contentDescription = null, modifier = Modifier.size(1.dp),
              onError = { controlErrors.incrementAndGet() })
          }
        }
      } } }
      for (text in listOf("参考ページ", "拒否リンク", "表示の最後")) waitForText(text)
      val link = layout("参考ページ").layoutInput.text
      assertEquals(url, (link.getLinkAnnotations(0, link.length).single().item as LinkAnnotation.Url).url)
      for (text in listOf("参考ページ", "拒否リンク")) {
        compose.onAllNodesWithText(text).onFirst().performScrollTo().performClick()
      }
      // Start the positive control only after Markdown parsing, placement, and link interaction.
      // Its completed request proves the active loader pipeline ran before the zero-request check.
      compose.runOnIdle { showControl.value = true }
      compose.waitUntil(10_000) { controlErrors.get() == 1 }
      compose.waitForIdle()
      InstrumentationRegistry.getInstrumentation().waitForIdleSync()
      compose.runOnIdle {
        assertEquals(listOf(url), opened)
        assertEquals("The image sentinel must observe its positive control", 1, controlRequests.get())
        assertEquals(0, imageRequests.get())
      }
    } finally {
      SingletonImageLoader.setUnsafe(previousLoader)
      recordingLoader.shutdown()
    }
  }

  private fun waitForText(text: String, substring: Boolean = false) {
    try {
      compose.waitUntil(10_000) {
        compose.onAllNodesWithText(text, substring = substring).fetchSemanticsNodes().isNotEmpty()
      }
    } catch (failure: ComposeTimeoutException) {
      // This class only renders the fictional strings declared above.
      throw AssertionError("Expected Markdown text: $text\n${compose.onRoot(useUnmergedTree = true).printToString()}", failure)
    }
  }

  private fun layout(text: String, substring: Boolean = false): TextLayoutResult {
    val results = mutableListOf<TextLayoutResult>()
    compose.onNode(hasText(text, substring = substring) and SemanticsMatcher.keyIsDefined(SemanticsActions.GetTextLayoutResult),
      useUnmergedTree = true).performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(results) }
    return results.single()
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureUi") != "true") return
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
