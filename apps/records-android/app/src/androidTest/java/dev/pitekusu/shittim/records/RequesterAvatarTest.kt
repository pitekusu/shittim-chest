package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import android.graphics.Color
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.ByteArrayOutputStream
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RequesterAvatarTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun fallbackPreservesEmojiAndCombiningCharacters() {
    for ((name, initial) in listOf(
      "error-fixture" to "e",
      "架空の利用者" to "架",
      "🥒架空の利用者" to "🥒",
      "👩🏽‍💻架空の利用者" to "👩🏽‍💻",
      "🇯🇵架空の利用者" to "🇯🇵",
      "か\u3099くう" to "か\u3099",
      "\u2764\uFE0F架空の利用者" to "\u2764\uFE0F",
      "  🥒架空の利用者" to "🥒",
      "" to "",
    )) assertEquals(initial, requesterAvatarInitial(name))
  }

  @ScreenTest
  @Test fun savedImageAndMissingOrInvalidImagesRemainVisible() {
    val bitmap = Bitmap.createBitmap(32, 32, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.MAGENTA) }
    val bytes = ByteArrayOutputStream().use { output ->
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)
      output.toByteArray()
    }
    bitmap.recycle()
    compose.activityRule.scenario.onActivity { it.setContent {
      ShittimTheme(darkTheme = true) {
        Surface {
          Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(24.dp)) {
            Text("依頼者アイコンの表示確認（架空データ）")
            for ((index, sample) in listOf(
              "🥒架空の利用者" to RecordAvatar(null, "lavender"),
              "👩🏽‍💻架空の利用者" to RecordAvatar(null, "cyan", bytes = byteArrayOf(1, 2, 3)),
              "error-fixture" to RecordAvatar(null, "pink"),
              "保存済み画像" to RecordAvatar(null, "cyan", bytes = bytes),
            ).withIndex()) {
              Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                RequesterAvatar(sample.first, sample.second, Modifier.testTag("avatar-$index"))
                Text(sample.first)
              }
            }
          }
        }
      }
    } }
    compose.waitUntil(5_000) {
      val image = compose.onNodeWithTag("avatar-3", useUnmergedTree = true).captureToImage().asAndroidBitmap()
      image.getPixel(image.width / 2, image.height / 2) == Color.MAGENTA
    }
    for (index in 0..2) {
      val image = compose.onNodeWithTag("avatar-$index", useUnmergedTree = true).captureToImage().asAndroidBitmap()
      val background = image.getPixel(image.width / 2, image.height / 8)
      assertTrue("The fallback character must remain visible", (image.width / 5 until image.width * 4 / 5).any { x ->
        (image.height / 5 until image.height * 4 / 5).any { y -> image.getPixel(x, y) != background }
      })
    }
    File(compose.activity.cacheDir, "requester-avatar-fixed.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
