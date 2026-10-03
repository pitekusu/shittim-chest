package dev.pitekusu.shittim.records

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Intent
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.Icon
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect

/** Permission is requested only from this explicit menu action, never automatically at startup. */
@Composable
internal fun RecordNotificationControls() {
  val context = LocalContext.current
  val generation by RecordNotifications.changes.collectAsState()
  val status = remember(generation) { RecordNotifications.status(context) }
  val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
    if (granted) RecordNotifications.enable(context) else synchronized(RecordNotifications.lock) {
      RecordNotificationSettings(context).denyPermission()
    }
  }
  LifecycleResumeEffect(Unit) {
    RecordNotifications.changed()
    onPauseOrDispose { }
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
    Surface(onClick = {
      when (status) {
        RecordNotificationStatus.PREPARING -> Unit
        RecordNotificationStatus.ENABLED, RecordNotificationStatus.REGISTERING -> RecordNotifications.disable(context)
        RecordNotificationStatus.PERMISSION_DENIED -> try {
          context.startActivity(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName))
        } catch (_: ActivityNotFoundException) { } catch (_: SecurityException) { }
        else -> if (RecordNotifications.permitted(context) || Build.VERSION.SDK_INT < 33) {
          RecordNotifications.enable(context)
        } else permission.launch(Manifest.permission.POST_NOTIFICATIONS)
      }
    }, enabled = status != RecordNotificationStatus.PREPARING,
      modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("record-notification-settings"),
      shape = MaterialTheme.shapes.medium, color = MaterialTheme.colorScheme.surfaceContainerLow) {
      ListItem(leadingContent = {
        Icon(painterResource(R.drawable.ic_record_notification), contentDescription = null)
      }, supportingContent = { Text(stringResource(description)) },
        colors = ListItemDefaults.colors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow)) {
        Text(stringResource(R.string.notification_setting))
      }
    }
    Text(stringResource(R.string.notification_privacy), style = MaterialTheme.typography.bodySmall,
      color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp))
  }
}
