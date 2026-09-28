package dev.pitekusu.shittim.records.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.R

/** Stored records written before participant slots were cached still resolve known names. */
internal fun participantVisualSlot(name: String, slot: String?): String? = when (slot) {
  "participant-a", "participant-b", "participant-c" -> slot
  null -> when (name) {
    "アロナ" -> "participant-a"
    "プラナ" -> "participant-b"
    "安倍晋三AI" -> "participant-c"
    else -> null
  }
  else -> null
}

@Composable
internal fun ShittimParticipantAvatar(name: String, slot: String?, size: Dp = 40.dp,
  crowned: Boolean = false, modifier: Modifier = Modifier) {
  val drawable = when (participantVisualSlot(name, slot)) {
    "participant-a" -> R.drawable.participant_a
    "participant-b" -> R.drawable.participant_b
    "participant-c" -> R.drawable.participant_c
    else -> null
  }
  val accent = shittimParticipantColor(name, slot)
  Box(modifier.size(size), contentAlignment = Alignment.Center) {
    Surface(Modifier.fillMaxSize(), shape = CircleShape,
      color = MaterialTheme.colorScheme.surfaceContainerHigh, border = BorderStroke(2.dp, accent)) {
      Box(contentAlignment = Alignment.Center) {
        if (drawable != null) {
          Image(painterResource(drawable), contentDescription = null,
            modifier = Modifier.fillMaxSize().clip(CircleShape), contentScale = ContentScale.Crop)
        } else {
          Icon(painterResource(R.drawable.ic_person), contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(size * 0.55f))
        }
      }
    }
    if (crowned) {
      Icon(painterResource(R.drawable.ic_crown), contentDescription = null,
        tint = accent, modifier = Modifier.align(Alignment.TopEnd).size(size * 0.42f))
    }
  }
}
