package dev.pitekusu.shittim.records

import android.content.ActivityNotFoundException
import android.net.Uri
import androidx.browser.customtabs.CustomTabsIntent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.auth.RECORDS_ORIGIN
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimSpacing

private val webDestinations = listOf(
  R.string.menu_insights to "/insights",
  R.string.menu_momotalk to "/momotalk",
  R.string.menu_memorial to "/memorial",
)
private val adminDestinations = listOf(
  R.string.menu_status to "/admin",
  R.string.menu_prompts to "/admin/prompts",
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun RecordsAppBar(onMenuClick: () -> Unit) {
  TopAppBar(title = {
    Text(stringResource(R.string.record_title), style = MaterialTheme.typography.titleMediumEmphasized)
  }, actions = {
    IconButton(onClick = onMenuClick, modifier = Modifier.testTag("records-menu-open")) {
      Icon(painterResource(R.drawable.ic_menu), contentDescription = stringResource(R.string.menu_open))
    }
  })
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun RecordsNavigationMenu(state: BootstrapScreen.State, onDismiss: () -> Unit) {
  val context = LocalContext.current
  var browserError by remember { mutableStateOf(false) }
  fun openWeb(path: String) {
    try {
      CustomTabsIntent.Builder().setShowTitle(true).build()
        .launchUrl(context, Uri.parse("$RECORDS_ORIGIN$path"))
      onDismiss()
    } catch (_: ActivityNotFoundException) {
      browserError = true
    } catch (_: SecurityException) {
      browserError = true
    }
  }
  ModalBottomSheet(onDismissRequest = onDismiss, modifier = Modifier.testTag("records-menu"),
    sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).testTag("records-menu-content")
      .padding(horizontal = ShittimSpacing.Medium, vertical = ShittimSpacing.Small),
      verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
      MenuItem(R.string.debate_requests, R.drawable.ic_add_debate,
        modifier = Modifier.testTag("debate-requests-open"), onClick = {
          onDismiss()
          state.eventSink(BootstrapScreen.Event.ShowDebateRequests)
        })
      ShittimSectionHeading(stringResource(R.string.menu_web))
      webDestinations.forEach { (label, path) ->
        MenuItem(label, R.drawable.ic_open_web, onClick = { openWeb(path) })
      }
      ShittimSectionHeading(stringResource(R.string.menu_admin))
      adminDestinations.forEach { (label, path) ->
        MenuItem(label, R.drawable.ic_open_web, onClick = { openWeb(path) })
      }
      if (browserError) Text(stringResource(R.string.menu_browser_unavailable),
        color = MaterialTheme.colorScheme.error)
      ShittimSectionHeading(stringResource(R.string.menu_settings))
      RecordNotificationControls()
      BootstrapThemeSelector(state.themeChoice) {
        state.eventSink(BootstrapScreen.Event.SelectTheme(it))
      }
      MenuItem(R.string.session_logout, R.drawable.ic_logout,
        modifier = Modifier.testTag("records-menu-logout"), onClick = {
        onDismiss()
        state.eventSink(BootstrapScreen.Event.Logout)
      })
    }
  }
}

@Composable
private fun MenuItem(label: Int, icon: Int, modifier: Modifier = Modifier, onClick: () -> Unit) {
  Surface(onClick = onClick, modifier = modifier.fillMaxWidth().heightIn(min = 48.dp),
    shape = MaterialTheme.shapes.medium, color = MaterialTheme.colorScheme.surfaceContainerLow) {
    ListItem(leadingContent = {
      Icon(painterResource(icon), contentDescription = null, Modifier.size(24.dp))
    }, colors = ListItemDefaults.colors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow)) {
      Text(stringResource(label))
    }
  }
}
