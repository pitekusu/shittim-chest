package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.auth.SessionNotice
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.ui.ShittimDisplayFont

@Composable
internal fun SessionPanel(state: SessionState, onEvent: (BootstrapScreen.Event) -> Unit) {
  OutlinedCard(Modifier.fillMaxWidth()) {
    Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
      Text("ACCOUNT", fontFamily = ShittimDisplayFont, color = MaterialTheme.colorScheme.primary)
      val title = when (state) {
        SessionState.Checking -> R.string.session_checking
        SessionState.Browser -> R.string.session_browser
        SessionState.SigningOut -> R.string.session_signing_out
        SessionState.Unavailable -> R.string.session_unavailable
        SessionState.StorageError -> R.string.session_storage_error
        is SessionState.SignedIn -> R.string.session_signed_in
        is SessionState.SignedOut -> R.string.session_signed_out
      }
      Text(stringResource(title), style = MaterialTheme.typography.titleLargeEmphasized,
        modifier = Modifier.semantics { heading(); liveRegion = LiveRegionMode.Polite })
      when (state) {
        SessionState.Checking, SessionState.SigningOut -> CircularProgressIndicator()
        SessionState.Browser -> {
          Text(stringResource(R.string.session_browser_hint))
          CircularProgressIndicator()
        }
        is SessionState.SignedOut -> {
          val explanation = when (state.notice) {
            SessionNotice.EXPIRED -> R.string.session_expired
            SessionNotice.CANCELLED -> R.string.session_cancelled
            SessionNotice.LOGIN_FAILED -> R.string.session_login_failed
            SessionNotice.BROWSER_UNAVAILABLE -> R.string.session_no_browser
            SessionNotice.LOCAL_LOGOUT -> R.string.session_local_logout
            null -> R.string.session_login_hint
          }
          Text(stringResource(explanation))
          Button(onClick = { onEvent(BootstrapScreen.Event.Login) }) {
            Text(stringResource(R.string.session_login))
          }
        }
        is SessionState.SignedIn -> {
          Text(state.user.displayName, style = MaterialTheme.typography.titleMedium)
          Text(stringResource(R.string.session_records_pending))
          FilledTonalButton(onClick = { onEvent(BootstrapScreen.Event.Logout) }) {
            Text(stringResource(R.string.session_logout))
          }
        }
        SessionState.Unavailable -> {
          Text(stringResource(R.string.session_retry_hint))
          Button(onClick = { onEvent(BootstrapScreen.Event.Retry) }) {
            Text(stringResource(R.string.session_retry))
          }
          FilledTonalButton(onClick = { onEvent(BootstrapScreen.Event.Logout) }) {
            Text(stringResource(R.string.session_local_logout_action))
          }
        }
        SessionState.StorageError -> {
          Text(stringResource(R.string.session_storage_hint))
          FilledTonalButton(onClick = { onEvent(BootstrapScreen.Event.Logout) }) {
            Text(stringResource(R.string.session_reset_storage))
          }
        }
      }
    }
  }
}
