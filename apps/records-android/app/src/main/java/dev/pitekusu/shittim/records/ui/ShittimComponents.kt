package dev.pitekusu.shittim.records.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.dp

internal object ShittimSpacing {
  val Small = 8.dp
  val Medium = 16.dp
  val Large = 24.dp
  val ExtraLarge = 32.dp
}

// Compose's Surface owns content color; wrappers only express repeated brand roles.
@Composable
internal fun ShittimPanel(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
  Surface(modifier.fillMaxWidth(), shape = MaterialTheme.shapes.large,
    color = MaterialTheme.colorScheme.surfaceContainerLow,
    contentColor = MaterialTheme.colorScheme.onSurface,
    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant)) {
    Column(Modifier.padding(ShittimSpacing.Large),
      verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium), content = content)
  }
}

@Composable
internal fun ShittimInset(content: @Composable ColumnScope.() -> Unit) {
  Surface(Modifier.fillMaxWidth(), shape = MaterialTheme.shapes.medium,
    color = MaterialTheme.colorScheme.surfaceContainerHigh,
    contentColor = MaterialTheme.colorScheme.onSurface) {
    Column(Modifier.padding(ShittimSpacing.Medium),
      verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small), content = content)
  }
}

@Composable
internal fun ShittimSectionHeading(
  title: String,
  modifier: Modifier = Modifier,
  kicker: String? = null,
  style: TextStyle = MaterialTheme.typography.titleMediumEmphasized,
) {
  Column(modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
    // Delogy is only used for short, fixed English brand labels, never Japanese content.
    if (kicker != null) Text(kicker, fontFamily = ShittimDisplayFont,
      style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
    Text(title, style = style, color = MaterialTheme.colorScheme.onSurface,
      modifier = Modifier.semantics { heading() })
  }
}

@Composable
internal fun shittimParticipantColor(name: String): Color = when (name) {
  "アロナ" -> MaterialTheme.colorScheme.primary
  "プラナ" -> MaterialTheme.colorScheme.secondary
  "安倍晋三AI" -> MaterialTheme.colorScheme.tertiary
  else -> MaterialTheme.colorScheme.onSurfaceVariant
}

@Composable
internal fun ShittimParticipantLabel(name: String, modifier: Modifier = Modifier) {
  val accent = shittimParticipantColor(name)
  Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small),
    verticalAlignment = Alignment.CenterVertically) {
    Box(Modifier.size(8.dp).background(accent, CircleShape))
    // A name accompanies the decorative accent so identity never depends on color.
    Text(name, style = MaterialTheme.typography.titleSmallEmphasized, color = accent,
      modifier = Modifier.weight(1f).semantics { heading() })
  }
}

@Composable
internal fun ShittimProgress(message: String, modifier: Modifier = Modifier) {
  Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small),
    verticalAlignment = Alignment.CenterVertically) {
    CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
    Text(message, style = MaterialTheme.typography.bodyMedium,
      color = MaterialTheme.colorScheme.onSurfaceVariant,
      modifier = Modifier.weight(1f).semantics { liveRegion = LiveRegionMode.Polite })
  }
}
