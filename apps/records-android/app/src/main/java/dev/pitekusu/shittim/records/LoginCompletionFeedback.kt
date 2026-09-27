package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Icon
import androidx.compose.material3.Snackbar
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalAccessibilityManager
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.withTimeoutOrNull

@Composable
internal fun LoginCompletionFeedback(completion: Int, signedIn: Boolean, modifier: Modifier = Modifier) {
  val host = remember { SnackbarHostState() }
  var consumed by rememberSaveable { mutableIntStateOf(0) }
  val message = stringResource(R.string.session_connected)
  val accessibility = LocalAccessibilityManager.current
  LaunchedEffect(completion, signedIn) {
    // A fresh process restarts the model's counter, but may restore this saved UI value.
    if (completion == 0) { consumed = 0; return@LaunchedEffect }
    if (!signedIn || completion == consumed) return@LaunchedEffect
    // Consume before showing: rotation/foreground changes must not replay this effect.
    consumed = completion
    val timeout = accessibility?.calculateRecommendedTimeoutMillis(2_000,
      containsIcons = true, containsText = true, containsControls = false) ?: 2_000L
    withTimeoutOrNull(timeout) {
      host.showSnackbar(message, duration = SnackbarDuration.Indefinite)
    }
  }
  // Standard Material motion follows Android's duration scale, including zero.
  // No full-screen interception, fake progress, authentication wait or extra API call.
  SnackbarHost(host, modifier) { data ->
    if (signedIn) Snackbar(Modifier.testTag("login-complete")) {
      Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(painterResource(R.drawable.ic_check), contentDescription = null, Modifier.size(24.dp))
        Text(data.visuals.message)
      }
    }
  }
}
