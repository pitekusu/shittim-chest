package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimTheme

@Composable
internal fun ReviewDemoBanner() {
  Surface(color = MaterialTheme.colorScheme.secondaryContainer, modifier = Modifier.fillMaxWidth()) {
    Text(stringResource(R.string.review_demo_banner), style = MaterialTheme.typography.labelSmall,
      modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp).testTag("review-demo-banner"))
  }
}

@Composable
internal fun ReviewDemoTransitionUi(failed: Boolean, retry: () -> Unit) {
  ShittimTheme {
    Surface(Modifier.fillMaxSize()) {
      Column(Modifier.fillMaxSize().safeDrawingPadding().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally) {
        Text(stringResource(if (failed) R.string.review_demo_storage_error else R.string.review_demo_preparing))
        if (failed) TextButton(onClick = retry) { Text(stringResource(R.string.session_retry)) }
        else CircularProgressIndicator()
      }
    }
  }
}
