package dev.pitekusu.shittim.records

import android.content.ActivityNotFoundException
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import com.mikepenz.markdown.m3.Markdown
import com.mikepenz.markdown.model.NoOpImageTransformerImpl
import java.net.URI
import java.net.URISyntaxException

// The record body is untrusted. Do not dispatch local/content/intent schemes from Markdown links.
internal fun allowedRecordLink(url: String): Boolean = try {
  val uri = URI(url)
  uri.scheme.equals("https", ignoreCase = true) && !uri.host.isNullOrBlank() &&
    uri.userInfo == null
} catch (_: URISyntaxException) {
  false
}

@Composable
internal fun RecordMarkdown(content: String, modifier: Modifier = Modifier) {
  val platformUriHandler = LocalUriHandler.current
  val safeUriHandler = remember(platformUriHandler) {
    object : UriHandler {
      override fun openUri(uri: String) {
        if (!allowedRecordLink(uri)) return
        try {
          platformUriHandler.openUri(uri)
        } catch (_: ActivityNotFoundException) {
          // No browser is installed; keep the record readable.
        }
      }
    }
  }
  CompositionLocalProvider(LocalUriHandler provides safeUriHandler) {
    // No image loader is connected: Markdown image URLs never fetch private or remote data.
    Markdown(content = content, modifier = modifier.fillMaxWidth(),
      imageTransformer = NoOpImageTransformerImpl())
  }
}
