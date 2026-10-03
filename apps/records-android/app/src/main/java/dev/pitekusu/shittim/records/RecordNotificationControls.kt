package dev.pitekusu.shittim.records

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.core.content.ContextCompat

/** One initial OS request, only after authentication; foreground changes also refresh registration. */
@Composable
internal fun RecordNotificationPermissionEffect(authorized: Boolean) {
  val context = LocalContext.current
  val generation by RecordNotifications.changes.collectAsState()
  var resumed by remember { mutableStateOf(false) }
  val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
    RecordNotifications.permissionResult(context, granted)
  }
  LifecycleResumeEffect(authorized) {
    resumed = true
    if (authorized) RecordNotifications.resumeAuthorizedSession(context)
    onPauseOrDispose { resumed = false }
  }
  LaunchedEffect(authorized, resumed, generation) {
    val request = synchronized(RecordNotifications.lock) {
      val eligible = authorized && resumed && !RecordNotifications.locallyRevoked &&
        RecordNotifications.configured(context) && Build.VERSION.SDK_INT >= 33
      val granted = Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context,
        Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
      RecordNotificationSettings(context).claimPermissionRequest(eligible, granted)
    }
    if (request) permission.launch(Manifest.permission.POST_NOTIFICATIONS)
  }
}

/** App ON/OFF and OS permission are independent: an OS denial never traps the switch ON. */
@Composable
internal fun RecordNotificationControls() {
  val context = LocalContext.current
  val generation by RecordNotifications.changes.collectAsState()
  val status = remember(generation) { RecordNotifications.status(context) }
  val enabled = remember(generation) { RecordNotificationSettings(context).optedIn }
  LifecycleResumeEffect(Unit) {
    RecordNotifications.changed()
    onPauseOrDispose { }
  }
  fun select(on: Boolean) {
    if (on) RecordNotifications.enable(context) else RecordNotifications.disable(context)
  }
  val description = when (status) {
    RecordNotificationStatus.PREPARING -> R.string.notification_preparing
    RecordNotificationStatus.DISABLED -> R.string.notification_disabled
    RecordNotificationStatus.PERMISSION_DENIED -> R.string.notification_denied
    RecordNotificationStatus.REGISTERING -> R.string.notification_registering
    RecordNotificationStatus.ENABLED -> R.string.notification_enabled
    RecordNotificationStatus.FAILED -> R.string.notification_failed
  }
  Column {
    Surface(onClick = { select(!enabled) }, enabled = status != RecordNotificationStatus.PREPARING,
      modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("record-notification-settings"),
      shape = MaterialTheme.shapes.medium, color = MaterialTheme.colorScheme.surfaceContainerLow) {
      val switchLabel = stringResource(R.string.notification_setting)
      ListItem(leadingContent = {
        Icon(painterResource(R.drawable.ic_record_notification), contentDescription = null)
      }, supportingContent = { Text(stringResource(description)) }, trailingContent = {
        Switch(checked = enabled, onCheckedChange = ::select,
          enabled = status != RecordNotificationStatus.PREPARING,
          modifier = Modifier.testTag("record-notification-switch").semantics { contentDescription = switchLabel })
      }, colors = ListItemDefaults.colors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow)) {
        Text(stringResource(R.string.notification_setting))
      }
    }
    if (status == RecordNotificationStatus.PERMISSION_DENIED) {
      TextButton(onClick = {
        try {
          context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName))
        } catch (_: ActivityNotFoundException) { } catch (_: SecurityException) { }
      }, modifier = Modifier.testTag("record-notification-os-settings")) {
        Text(stringResource(R.string.notification_open_settings))
      }
    } else if (status == RecordNotificationStatus.FAILED) {
      TextButton(onClick = { RecordNotificationRegistration.schedule(context, replace = true) }) {
        Text(stringResource(R.string.session_retry))
      }
    }
    Text(stringResource(R.string.notification_privacy), style = MaterialTheme.typography.bodySmall,
      color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp))
  }
}
