package dev.pitekusu.shittim.records

import androidx.compose.animation.core.animateDpAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.material3.ButtonGroup
import androidx.compose.material3.ButtonGroupDefaults
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.ToggleButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp

@Composable
internal fun RecordOrderSwitch(selected: RecordOrder, onSelect: (RecordOrder) -> Unit) {
  val labels = listOf(stringResource(R.string.record_sort_newest), stringResource(R.string.record_sort_oldest))
  val names = listOf("NEW", "OLD")
  Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
    ButtonGroup(overflowIndicator = { ButtonGroupDefaults.OverflowIndicator(it) },
      horizontalArrangement = Arrangement.spacedBy(ButtonGroupDefaults.ConnectedSpaceBetween),
      verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth().selectableGroup()) {
      RecordOrder.entries.forEach { order ->
        // Standard ToggleButton owns interaction, checked semantics and shape morphing.
        // Custom content only lets the Japanese explanation wrap at large text sizes.
        customItem(buttonGroupContent = {
          val interactions = remember { MutableInteractionSource() }
          ToggleButton(checked = selected == order, onCheckedChange = { if (it) onSelect(order) },
            modifier = Modifier.weight(1f).animateWidth(interactions).heightIn(min = 48.dp)
              .testTag("order-${order.name}"), interactionSource = interactions,
            shapes = if (order == RecordOrder.Newest) ButtonGroupDefaults.connectedLeadingButtonShapes()
              else ButtonGroupDefaults.connectedTrailingButtonShapes(), contentPadding = PaddingValues(12.dp)) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
              Text(names[order.ordinal], style = MaterialTheme.typography.labelLarge)
              Text(labels[order.ordinal], style = MaterialTheme.typography.labelMedium)
            }
          }
        }, menuContent = { menu ->
          DropdownMenuItem(text = { Text("${names[order.ordinal]} · ${labels[order.ordinal]}") },
            onClick = { onSelect(order); menu.dismiss() }, leadingIcon = if (selected == order) {
              { Icon(painterResource(R.drawable.ic_check), contentDescription = null, Modifier.size(18.dp)) }
            } else null)
        })
      }
    }
    // A decorative position cue only; it does not implement selection or input handling.
    BoxWithConstraints(Modifier.fillMaxWidth().height(4.dp)) {
      val half = maxWidth / 2
      val position by animateDpAsState(if (selected == RecordOrder.Newest) 0.dp else half,
        animationSpec = MaterialTheme.motionScheme.fastSpatialSpec(), label = "record-order-position")
      Box(Modifier.offset(x = position).width(half).height(4.dp)
        .background(MaterialTheme.colorScheme.primary, MaterialTheme.shapes.extraSmall))
    }
  }
}
