package dev.pitekusu.shittim.records

import android.content.ActivityNotFoundException
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import com.slack.circuit.runtime.CircuitUiEvent
import com.slack.circuit.runtime.CircuitUiState
import com.slack.circuit.runtime.presenter.Presenter
import com.slack.circuit.runtime.screen.Screen
import coil3.SingletonImageLoader
import androidx.compose.ui.platform.LocalContext
import dev.pitekusu.shittim.records.auth.MobileLoginContract
import dev.pitekusu.shittim.records.auth.MobileLoginStatus
import dev.pitekusu.shittim.records.auth.MobileLoginStep
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.auth.SessionState
import dev.zacsweers.metro.Inject

internal enum class ThemeChoice {
  System,
  Light,
  Dark,
}

// A single fixed destination; no navigation stack or screen serialization is needed yet.
internal data object BootstrapScreen : Screen {
  class State(
    val themeChoice: ThemeChoice,
    val session: SessionState = SessionState.SignedOut(),
    val records: RecordListState = RecordListState.Idle,
    val record: RecordPreviewState = RecordPreviewState.Idle,
    val selectedRecordId: String? = null,
    val eventSink: (Event) -> Unit,
  ) : CircuitUiState

  sealed interface Event : CircuitUiEvent {
    data class SelectTheme(val choice: ThemeChoice) : Event
    data object Login : Event
    data object Logout : Event
    data object Retry : Event
    data object RetryRecord : Event
    data object RetryRecords : Event
    data class OpenRecord(val recordId: String) : Event
    data object CloseRecord : Event
  }
}

@Inject
internal class BootstrapPresenter(
  private val session: MobileSessionModel,
) : Presenter<BootstrapScreen.State> {
  @Composable
  override fun present(): BootstrapScreen.State {
    // Restore the presentation state, without persisting a device/account preference.
    var themeChoice by rememberSaveable { mutableStateOf(ThemeChoice.System) }
    val sessionState by session.state.collectAsState()
    val context = LocalContext.current
    val signedIn = sessionState is SessionState.SignedIn
    DisposableEffect(signedIn) {
      // Discard presigned avatar bitmaps when authentication leaves this screen.
      onDispose { if (signedIn) SingletonImageLoader.get(context).memoryCache?.clear() }
    }
    val launcher = rememberLauncherForActivityResult(MobileLoginContract(), session::loginResult)
    val records = remember { RecordsReadClient() }
    DisposableEffect(records) { onDispose { records.close() } }
    // The preview belongs to this exact session, including after an account switch.
    var record by remember(sessionState) { mutableStateOf<RecordPreviewState>(RecordPreviewState.Idle) }
    var recordRetry by remember { mutableStateOf(0) }
    var recordList by remember(sessionState) { mutableStateOf<RecordListState>(RecordListState.Idle) }
    var listRetry by remember { mutableStateOf(0) }
    var selectedRecordId by remember(sessionState) {
      mutableStateOf((sessionState as? SessionState.SignedIn)?.returnTo
        ?.takeIf { it.startsWith("/records/") }?.removePrefix("/records/"))
    }
    LaunchedEffect(sessionState, listRetry) {
      recordList = RecordListState.Idle
      if (sessionState is SessionState.SignedIn) {
        recordList = RecordListState.Loading
        recordList = try {
          val page = session.withAuthorizedToken(records::recentRecords)
          when {
            page == null -> RecordListState.Idle
            page.items.isEmpty() -> RecordListState.Empty
            else -> RecordListState.Ready(page)
          }
        } catch (error: RecordReadException) {
          if (error.failure == RecordReadFailure.AUTH_REQUIRED) session.onForeground()
          RecordListState.Error(error.failure)
        }
      }
    }
    LaunchedEffect(sessionState, selectedRecordId, recordRetry) {
      record = RecordPreviewState.Idle
      val signedIn = sessionState as? SessionState.SignedIn
      if (signedIn != null && selectedRecordId != null) {
        record = RecordPreviewState.Loading
        record = try {
          val result = session.withAuthorizedToken {
            records.firstRecord(it, "/records/$selectedRecordId")
          }
          when (result) {
            null -> RecordPreviewState.Idle
            RecordReadResult.Empty -> RecordPreviewState.Error(RecordReadFailure.INVALID_RESPONSE)
            is RecordReadResult.Found -> RecordPreviewState.Ready(result.preview)
          }
        } catch (error: RecordReadException) {
          if (error.failure == RecordReadFailure.AUTH_REQUIRED) session.onForeground()
          RecordPreviewState.Error(error.failure)
        }
      }
    }
    return BootstrapScreen.State(themeChoice, sessionState, recordList, record, selectedRecordId) { event ->
      when (event) {
        is BootstrapScreen.Event.SelectTheme -> themeChoice = event.choice
        BootstrapScreen.Event.Login -> if (session.beginLogin()) {
          try { launcher.launch(session.loginDestination) }
          catch (_: ActivityNotFoundException) {
            session.loginResult(MobileLoginStep.Finished(MobileLoginStatus.BROWSER_UNAVAILABLE))
          }
        }
        BootstrapScreen.Event.Logout -> session.logout()
        BootstrapScreen.Event.Retry -> session.retry()
        BootstrapScreen.Event.RetryRecord -> recordRetry++
        BootstrapScreen.Event.RetryRecords -> listRetry++
        is BootstrapScreen.Event.OpenRecord -> if (
          (recordList as? RecordListState.Ready)?.page?.items?.any { it.recordId == event.recordId } == true
        ) selectedRecordId = event.recordId
        BootstrapScreen.Event.CloseRecord -> selectedRecordId = null
      }
    }
  }
}
