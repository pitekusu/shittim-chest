package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.ButtonGroup
import androidx.compose.material3.ButtonGroupDefaults
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp

@Composable
internal fun RecordQueryControls(query: RecordListQuery, onEvent: (BootstrapScreen.Event) -> Unit) {
  val keyboard = LocalSoftwareKeyboardController.current
  val orderLabels = listOf(stringResource(R.string.record_sort_newest), stringResource(R.string.record_sort_oldest))
  Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(12.dp)) {
    OutlinedTextField(
      value = query.text,
      onValueChange = { onEvent(BootstrapScreen.Event.SearchRecords(it)) },
      label = { Text(stringResource(R.string.record_search_label)) },
      supportingText = { Text(stringResource(R.string.record_search_hint)) },
      singleLine = true,
      keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, imeAction = ImeAction.Search),
      keyboardActions = KeyboardActions(onSearch = { keyboard?.hide() }),
      modifier = Modifier.fillMaxWidth().testTag("record-search"),
    )
    Text(stringResource(R.string.record_filter_winner), style = MaterialTheme.typography.labelLarge,
      color = MaterialTheme.colorScheme.onSurfaceVariant)
    // Wrap rather than clip or shrink labels at 320dp / large text.
    FlowRow(Modifier.selectableGroup(), horizontalArrangement = Arrangement.spacedBy(8.dp),
      verticalArrangement = Arrangement.spacedBy(4.dp)) {
      RecordWinner.entries.forEach { winner ->
        val selected = query.winner == winner
        FilterChip(selected = selected,
          onClick = { onEvent(BootstrapScreen.Event.SelectWinner(winner)) },
          label = { Text(winner.nameInRecord ?: stringResource(R.string.record_filter_all)) },
          shapes = FilterChipDefaults.shapes(),
          leadingIcon = if (selected) {
            { Icon(painterResource(R.drawable.ic_check), contentDescription = null, Modifier.size(18.dp)) }
          } else null,
          modifier = Modifier.heightIn(min = 48.dp).testTag("winner-${winner.name}"),
        )
      }
    }
    Text(stringResource(R.string.record_sort_label), style = MaterialTheme.typography.labelLarge,
      color = MaterialTheme.colorScheme.onSurfaceVariant)
    ButtonGroup(overflowIndicator = { ButtonGroupDefaults.OverflowIndicator(it) },
      modifier = Modifier.selectableGroup()) {
      RecordOrder.entries.forEach { order ->
        toggleableItem(checked = query.order == order,
          label = orderLabels[order.ordinal],
          onCheckedChange = { if (it) onEvent(BootstrapScreen.Event.SelectOrder(order)) },
          icon = if (query.order == order) {
            { Icon(painterResource(R.drawable.ic_check), contentDescription = null, Modifier.size(18.dp)) }
          } else null)
      }
    }
    if (!query.isDefault || query.text.isNotEmpty()) {
      TextButton(onClick = { onEvent(BootstrapScreen.Event.ClearRecordQuery) }) {
        Text(stringResource(R.string.record_search_reset))
      }
    }
  }
}
