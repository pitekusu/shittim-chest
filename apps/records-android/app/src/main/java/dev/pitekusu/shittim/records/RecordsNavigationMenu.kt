package dev.pitekusu.shittim.records

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimSpacing

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
  var demoAction by remember { mutableStateOf<BootstrapScreen.Event?>(null) }
  if (demoAction != null) {
    val action = requireNotNull(demoAction)
    AlertDialog(onDismissRequest = { demoAction = null },
      title = { Text(stringResource(if (action == BootstrapScreen.Event.ResetDemo) R.string.review_demo_reset else R.string.review_demo_exit)) },
      text = { Text(stringResource(R.string.review_demo_reset_hint)) },
      confirmButton = { TextButton(onClick = {
        demoAction = null
        onDismiss()
        state.eventSink(action)
      }, modifier = Modifier.testTag("review-demo-confirm")) { Text(stringResource(R.string.review_demo_confirm)) } },
      dismissButton = { TextButton(onClick = { demoAction = null }) { Text(stringResource(R.string.review_demo_cancel)) } })
  }
  ModalBottomSheet(onDismissRequest = onDismiss, modifier = Modifier.testTag("records-menu"),
    sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)) {
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).testTag("records-menu-content")
      .padding(horizontal = ShittimSpacing.Medium, vertical = ShittimSpacing.Small),
      verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
      DebateRequestsMenuItem(onClick = {
        onDismiss()
        state.eventSink(BootstrapScreen.Event.ShowDebateRequests)
      })
      MenuItem(R.string.menu_insights, R.drawable.ic_debate_history,
        modifier = Modifier.testTag("insights-open"), onClick = {
          onDismiss()
          state.eventSink(BootstrapScreen.Event.ShowInsights)
        })
      if (state.demoEntryId == null) {
        ShittimSectionHeading(stringResource(R.string.menu_web))
        RecordsWebPage.entries.filterNot { it.administrator }.forEach { page ->
          MenuItem(page.title, R.drawable.ic_open_web,
            modifier = Modifier.testTag("web-page-${page.name}"), onClick = {
              state.eventSink(BootstrapScreen.Event.OpenWebPage(page))
            })
        }
        ShittimSectionHeading(stringResource(R.string.menu_admin))
        RecordsWebPage.entries.filter { it.administrator }.forEach { page ->
          MenuItem(page.title, R.drawable.ic_open_web,
            modifier = Modifier.testTag("web-page-${page.name}"), onClick = {
              state.eventSink(BootstrapScreen.Event.OpenWebPage(page))
            })
        }
      }
      ShittimSectionHeading(stringResource(R.string.menu_public_information))
      RecordsPublicPage.entries.forEach { page ->
        MenuItem(page.title, R.drawable.ic_open_web,
          modifier = Modifier.testTag("public-page-${page.name}"), onClick = {
            state.eventSink(BootstrapScreen.Event.OpenPublicPage(page))
          })
      }
      if (state.publicPageError) Text(stringResource(R.string.menu_browser_unavailable),
        color = MaterialTheme.colorScheme.error)
      ShittimSectionHeading(stringResource(R.string.menu_settings))
      RecordNotificationControls(demoEntryId = state.demoEntryId)
      BootstrapThemeSelector(state.themeChoice) {
        state.eventSink(BootstrapScreen.Event.SelectTheme(it))
      }
      if (state.demoEntryId != null) {
        Text(stringResource(R.string.review_demo_external_links), style = MaterialTheme.typography.bodySmall)
        MenuItem(R.string.review_demo_reset, R.drawable.ic_arrow_back,
          modifier = Modifier.testTag("review-demo-reset"), onClick = { demoAction = BootstrapScreen.Event.ResetDemo })
        MenuItem(R.string.review_demo_exit, R.drawable.ic_logout,
          modifier = Modifier.testTag("review-demo-exit"), onClick = { demoAction = BootstrapScreen.Event.ExitDemo })
      } else MenuItem(R.string.session_logout, R.drawable.ic_logout,
        modifier = Modifier.testTag("records-menu-logout"), onClick = {
        onDismiss()
        state.eventSink(BootstrapScreen.Event.Logout)
      })
    }
  }
}

@Composable
private fun DebateRequestsMenuItem(onClick: () -> Unit) {
  val colors = MaterialTheme.colorScheme
  Surface(onClick = onClick, modifier = Modifier.fillMaxWidth().testTag("debate-requests-open"),
    shape = MaterialTheme.shapes.large, color = colors.primaryContainer,
    contentColor = colors.onPrimaryContainer, border = BorderStroke(1.dp, colors.primary.copy(alpha = .45f))) {
    ListItem(leadingContent = {
      Surface(shape = CircleShape, color = colors.primary, contentColor = colors.onPrimary) {
        Icon(painterResource(R.drawable.ic_debate_history), contentDescription = null,
          modifier = Modifier.padding(12.dp).size(28.dp))
      }
    }, supportingContent = {
      Text(stringResource(R.string.debate_requests_description), style = MaterialTheme.typography.bodyMedium)
    }, trailingContent = {
      Icon(painterResource(R.drawable.ic_expand_more), contentDescription = null,
        modifier = Modifier.size(24.dp).rotate(-90f))
    }, colors = ListItemDefaults.colors(containerColor = colors.primaryContainer,
      headlineColor = colors.onPrimaryContainer, supportingColor = colors.onPrimaryContainer,
      trailingIconColor = colors.primary)) {
      Text(stringResource(R.string.debate_requests), style = MaterialTheme.typography.titleLargeEmphasized)
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
