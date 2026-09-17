package dev.pitekusu.shittim.records

import android.content.res.Configuration
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SegmentedListItem
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimDisplayFont
import dev.pitekusu.shittim.records.ui.ShittimEmblem
import dev.pitekusu.shittim.records.ui.ShittimTheme

@Preview(name = "Light", widthDp = 360, heightDp = 800)
@Preview(name = "Dark", uiMode = Configuration.UI_MODE_NIGHT_YES, widthDp = 360, heightDp = 800)
@Preview(name = "Small / large text", widthDp = 320, heightDp = 640, fontScale = 2f)
@Preview(name = "Expanded", widthDp = 1000, heightDp = 700)
@Composable
fun BootstrapScreen() {
  // Restore across rotation/resizing, but do not persist an account or device preference.
  var themeChoice by rememberSaveable { mutableStateOf(ThemeChoice.System) }
  val darkTheme =
    when (themeChoice) {
      ThemeChoice.System -> isSystemInDarkTheme()
      ThemeChoice.Light -> false
      ThemeChoice.Dark -> true
    }
  ShittimTheme(darkTheme) {
    ShittimBackdrop {
      BoxWithConstraints(Modifier.fillMaxSize().safeDrawingPadding()) {
        val layout = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(24.dp)
        // Large text keeps a single readable column even in a wide window.
        if (maxWidth >= 840.dp && LocalDensity.current.fontScale < 1.5f) {
          Row(
            layout,
            horizontalArrangement = Arrangement.spacedBy(48.dp, Alignment.CenterHorizontally),
            verticalAlignment = Alignment.CenterVertically,
          ) {
            BootstrapHeader(Modifier.weight(1f, fill = false).widthIn(max = 400.dp))
            BootstrapControls(
              themeChoice,
              { themeChoice = it },
              Modifier.weight(1f, fill = false).widthIn(max = 480.dp),
            )
          }
        } else {
          Column(
            layout,
            verticalArrangement = Arrangement.spacedBy(32.dp, Alignment.CenterVertically),
            horizontalAlignment = Alignment.CenterHorizontally,
          ) {
            BootstrapHeader(Modifier.widthIn(max = 560.dp).fillMaxWidth())
            BootstrapControls(
              themeChoice,
              { themeChoice = it },
              Modifier.widthIn(max = 560.dp).fillMaxWidth(),
            )
          }
        }
      }
    }
  }
}

@Composable
private fun BootstrapHeader(modifier: Modifier) {
  Column(modifier, verticalArrangement = Arrangement.spacedBy(16.dp)) {
    ShittimEmblem(Modifier.size(72.dp))
    Text(
      stringResource(R.string.brand_title),
      fontFamily = ShittimDisplayFont,
      style = MaterialTheme.typography.headlineMedium,
      color = MaterialTheme.colorScheme.primary,
    )
    Text(
      stringResource(R.string.app_name),
      style = MaterialTheme.typography.headlineLargeEmphasized,
      color = MaterialTheme.colorScheme.onSurface,
      modifier = Modifier.semantics { heading() },
    )
    Text(
      stringResource(R.string.bootstrap_title),
      style = MaterialTheme.typography.titleLarge,
      color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
  }
}

@Composable
private fun BootstrapControls(
  themeChoice: ThemeChoice,
  onThemeChange: (ThemeChoice) -> Unit,
  modifier: Modifier,
) {
  Column(modifier, verticalArrangement = Arrangement.spacedBy(24.dp)) {
    BootstrapThemeSelector(themeChoice, onThemeChange)
    Text(
      stringResource(R.string.bootstrap_scope),
      style = MaterialTheme.typography.titleMediumEmphasized,
      color = MaterialTheme.colorScheme.onSurface,
      modifier = Modifier.semantics { heading() },
    )
    // Informational rows, not disabled navigation to screens that do not exist yet.
    Column(verticalArrangement = Arrangement.spacedBy(ListItemDefaults.SegmentedGap)) {
      SegmentedListItem(
        shapes = ListItemDefaults.segmentedShapes(index = 0, count = 2),
        overlineContent = { Text(stringResource(R.string.bootstrap_available)) },
        supportingContent = { Text(stringResource(R.string.bootstrap_preview_description)) },
      ) {
        Text(stringResource(R.string.bootstrap_preview))
      }
      SegmentedListItem(
        shapes = ListItemDefaults.segmentedShapes(index = 1, count = 2),
        overlineContent = { Text(stringResource(R.string.bootstrap_planned)) },
        supportingContent = { Text(stringResource(R.string.bootstrap_records_description)) },
      ) {
        Text(stringResource(R.string.bootstrap_records))
      }
    }
  }
}
