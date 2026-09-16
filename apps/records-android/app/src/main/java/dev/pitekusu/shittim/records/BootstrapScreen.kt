package dev.pitekusu.shittim.records

import android.content.res.Configuration
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CutCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimDisplayFont
import dev.pitekusu.shittim.records.ui.ShittimEmblem
import dev.pitekusu.shittim.records.ui.ShittimTheme

@Preview(name = "Light", showBackground = true, widthDp = 360, heightDp = 800)
@Preview(name = "Dark", uiMode = Configuration.UI_MODE_NIGHT_YES, widthDp = 360, heightDp = 800)
@Preview(name = "Small / large text", widthDp = 320, heightDp = 640, fontScale = 1.5f)
@Composable
fun BootstrapScreen() {
  val systemDark = isSystemInDarkTheme()
  // A session-only design preview, not an account preference or a fake login action.
  var darkTheme by remember(systemDark) { mutableStateOf(systemDark) }
  ShittimTheme(darkTheme) {
    val colors = MaterialTheme.colorScheme
    ShittimBackdrop {
      Column(
        modifier =
          Modifier.fillMaxSize()
            .safeDrawingPadding()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 24.dp, vertical = 40.dp),
        verticalArrangement = Arrangement.spacedBy(32.dp, Alignment.CenterVertically),
        horizontalAlignment = Alignment.CenterHorizontally,
      ) {
        Column(
          Modifier.widthIn(max = 480.dp).fillMaxWidth(),
          verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
          Text(
            stringResource(R.string.brand_title),
            fontFamily = ShittimDisplayFont,
            style = MaterialTheme.typography.headlineMedium,
            color = colors.primary,
          )
          Text(
            stringResource(R.string.app_name),
            style = MaterialTheme.typography.headlineLarge,
            fontWeight = FontWeight.Bold,
            color = colors.onBackground,
            modifier = Modifier.semantics { heading() },
          )
        }
        Surface(
          modifier = Modifier.widthIn(max = 480.dp).fillMaxWidth(),
          shape = CutCornerShape(topEnd = 24.dp, bottomStart = 12.dp),
          color = colors.surfaceContainer.copy(alpha = 0.96f),
          border = BorderStroke(1.dp, colors.outlineVariant),
        ) {
          Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(24.dp)) {
            ShittimEmblem(Modifier.size(72.dp))
            Text(
              stringResource(R.string.bootstrap_title),
              style = MaterialTheme.typography.headlineSmall,
              fontWeight = FontWeight.Bold,
              modifier = Modifier.semantics { heading() },
            )
            Text(
              stringResource(R.string.bootstrap_message),
              style = MaterialTheme.typography.bodyLarge,
              color = colors.onSurfaceVariant,
            )
            HorizontalDivider(color = colors.outlineVariant)
            Button(
              onClick = { darkTheme = !darkTheme },
              shapes = ButtonDefaults.shapes(),
              modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp),
            ) {
              Text(stringResource(if (darkTheme) R.string.preview_light else R.string.preview_dark))
            }
          }
        }
      }
    }
  }
}
