package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.material3.ButtonGroup
import androidx.compose.material3.ButtonGroupDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

internal enum class ThemeChoice {
  System,
  Light,
  Dark,
}

@Composable
internal fun BootstrapThemeSelector(selected: ThemeChoice, onSelect: (ThemeChoice) -> Unit) {
  val labels =
    listOf(
      stringResource(R.string.preview_system),
      stringResource(R.string.preview_light),
      stringResource(R.string.preview_dark),
    )
  Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
    Text(
      stringResource(R.string.preview_theme),
      style = MaterialTheme.typography.titleMediumEmphasized,
      color = MaterialTheme.colorScheme.onSurface,
      modifier = Modifier.semantics { heading() },
    )
    ButtonGroup(
      overflowIndicator = { ButtonGroupDefaults.OverflowIndicator(it) },
      modifier = Modifier.fillMaxWidth().selectableGroup(),
    ) {
      ThemeChoice.entries.forEachIndexed { index, choice ->
        toggleableItem(
          checked = choice == selected,
          label = labels[index],
          // A single choice must remain selected; clicking it again is a no-op.
          onCheckedChange = { if (it) onSelect(choice) },
          icon =
            if (choice == selected) {
              {
                Icon(
                  painterResource(R.drawable.ic_check),
                  contentDescription = null,
                  modifier = Modifier.size(18.dp),
                )
              }
            } else null,
          weight = 1f,
        )
      }
    }
    Text(
      stringResource(R.string.preview_theme_hint),
      style = MaterialTheme.typography.bodyMedium,
      color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
  }
}
