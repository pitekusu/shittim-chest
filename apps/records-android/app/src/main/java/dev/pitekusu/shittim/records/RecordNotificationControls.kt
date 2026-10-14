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
import androidx.compose.material3.OutlinedButton
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
internal fun RecordNotificationPermissionEffect(authorized: Boolean, demoEntryId: String? = null) {
  val context = LocalContext.current
  val generation by RecordNotifications.changes.collectAsState()
  val demoGeneration by DemoNotifications.changes.collectAsState()
  var resumed by remember { mutableStateOf(false) }
  val permission = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
    if (demoEntryId == null) RecordNotifications.permissionResult(context, granted)
    else DemoNotifications.permissionResult(context, demoEntryId, granted)
  }
  LifecycleResumeEffect(authorized, demoEntryId) {
    resumed = true
    if (demoEntryId != null) DemoNotifications.refresh()
    else if (authorized) RecordNotifications.resumeAuthorizedSession(context)
    onPauseOrDispose { resumed = false }
  }
  LaunchedEffect(authorized, resumed, generation, demoEntryId, demoGeneration) {
    val request = synchronized(RecordNotifications.lock) {
      val granted = Build.VERSION.SDK_INT < 33 || ContextCompat.checkSelfPermission(context,
        Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
      if (demoEntryId != null) resumed && DemoNotifications.claimPermissionRequest(context, demoEntryId, granted)
      else {
        val eligible = authorized && resumed && ReviewDemoModeStore.productionAllowed(context) &&
          !RecordNotifications.locallyRevoked && RecordNotifications.configured(context) && Build.VERSION.SDK_INT >= 33
        RecordNotificationSettings(context).claimPermissionRequest(eligible, granted)
      }
    }
    if (request) permission.launch(Manifest.permission.POST_NOTIFICATIONS)
  }
}

/** App ON/OFF and OS permission are independent: an OS denial never traps the switch ON. */
@Composable
internal fun RecordNotificationControls(demoEntryId: String? = null) {
  val context = LocalContext.current
  val generation by RecordNotifications.changes.collectAsState()
  val demoGeneration by DemoNotifications.changes.collectAsState()
  val status = remember(generation, demoGeneration, demoEntryId) {
    if (demoEntryId == null) RecordNotifications.status(context) else DemoNotifications.status(context, demoEntryId)
  }
  val enabled = remember(generation, demoGeneration, demoEntryId) {
    if (demoEntryId == null) RecordNotificationSettings(context).optedIn else DemoNotifications.optedIn(context, demoEntryId)
  }
  val diagnostic = remember(generation, demoEntryId) { if (demoEntryId == null) RecordNotificationSettings(context) else null }
  val vibrationUpgrade = remember(generation, demoEntryId) {
    demoEntryId == null && ReviewDemoModeStore.productionAllowed(context) && needsVibrationUpgrade(context)
  }
  LifecycleResumeEffect(demoEntryId) {
    if (demoEntryId == null) RecordNotifications.changed() else DemoNotifications.refresh()
    onPauseOrDispose { }
  }
  fun select(on: Boolean) {
    if (demoEntryId != null) {
      if (on) DemoNotifications.enable(context, demoEntryId) else DemoNotifications.disable(context, demoEntryId)
    } else if (on) RecordNotifications.enable(context) else RecordNotifications.disable(context)
  }
  val description = when (status) {
    RecordNotificationStatus.PREPARING -> R.string.notification_preparing
    RecordNotificationStatus.DISABLED -> R.string.notification_disabled
    RecordNotificationStatus.PERMISSION_DENIED -> R.string.notification_denied
    RecordNotificationStatus.REGISTERING -> R.string.notification_registering
    RecordNotificationStatus.ENABLED -> if (demoEntryId == null) R.string.notification_enabled else R.string.demo_notification_enabled
    RecordNotificationStatus.FAILED -> R.string.notification_failed
  }
  Column {
    Surface(onClick = { select(!enabled) }, enabled = status != RecordNotificationStatus.PREPARING,
      modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("record-notification-settings"),
      shape = MaterialTheme.shapes.medium, color = MaterialTheme.colorScheme.surfaceContainerLow) {
      val switchLabel = stringResource(if (demoEntryId == null) R.string.notification_setting else R.string.demo_notification_setting)
      ListItem(leadingContent = {
        Icon(painterResource(R.drawable.ic_record_notification), contentDescription = null)
      }, supportingContent = { Text(stringResource(description)) }, trailingContent = {
        Switch(checked = enabled, onCheckedChange = ::select,
          enabled = status != RecordNotificationStatus.PREPARING,
          modifier = Modifier.testTag("record-notification-switch").semantics { contentDescription = switchLabel })
      }, colors = ListItemDefaults.colors(containerColor = MaterialTheme.colorScheme.surfaceContainerLow)) {
        Text(switchLabel)
      }
    }
    if (vibrationUpgrade) {
      OutlinedButton(onClick = { upgradeRecordNotificationVibration(context) },
        modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("record-notification-vibration")) {
        Text(stringResource(R.string.notification_vibration_upgrade))
      }
      Text(stringResource(R.string.notification_vibration_description),
        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp))
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
      TextButton(onClick = { RecordNotifications.retryRegistration(context) }) {
        Text(stringResource(R.string.session_retry))
      }
    }
    if (status == RecordNotificationStatus.FAILED || status == RecordNotificationStatus.REGISTERING) {
      val stage = diagnostic?.failureStage
      val category = diagnostic?.failureCategory
      if (stage != null && category != null) Text(stringResource(R.string.notification_diagnostic,
        stringResource(stage.label), stringResource(category.label), diagnostic?.failureAttempt ?: 1),
        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(horizontal = 12.dp).testTag("record-notification-diagnostic"))
    }
    Text(stringResource(if (demoEntryId == null) R.string.notification_privacy else R.string.demo_notification_privacy), style = MaterialTheme.typography.bodySmall,
      color = MaterialTheme.colorScheme.onSurfaceVariant, modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp))
  }
}
