package dev.pitekusu.shittim.records.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp

@Composable
fun ShittimBackdrop(content: @Composable BoxScope.() -> Unit) {
  val colors = MaterialTheme.colorScheme
  Box(
    Modifier.fillMaxSize()
      .background(
        Brush.linearGradient(listOf(colors.background, colors.primaryContainer, colors.surface))
      )
  ) {
    // Decoration only: no accessibility nodes, perpetual motion, blur, or image assets.
    Canvas(Modifier.matchParentSize()) {
      val step = 56.dp.toPx()
      val line = colors.outlineVariant.copy(alpha = 0.32f)
      for (column in 0..(size.width / step).toInt()) {
        val x = column * step
        drawLine(line, Offset(x, 0f), Offset(x, size.height))
      }
      for (row in 0..(size.height / step).toInt()) {
        val y = row * step
        drawLine(line, Offset(0f, y), Offset(size.width, y))
      }
      val radius = size.width * 0.65f
      drawCircle(line, radius, Offset(size.width, size.height * 0.12f), style = Stroke(1.dp.toPx()))
      drawCircle(
        line,
        radius + 16.dp.toPx(),
        Offset(size.width, size.height * 0.12f),
        style = Stroke(1.dp.toPx()),
      )
    }
    content()
  }
}

@Composable
fun ShittimEmblem(modifier: Modifier = Modifier) {
  val colors = MaterialTheme.colorScheme
  Canvas(modifier) {
    val radius = size.minDimension / 2f
    drawCircle(colors.primary.copy(alpha = 0.08f), radius)
    drawCircle(colors.outlineVariant, radius * 0.86f, style = Stroke(1.dp.toPx()))
    drawArc(
      colors.primary,
      -80f,
      220f,
      false,
      topLeft = Offset(radius * 0.08f, radius * 0.08f),
      size = Size(radius * 1.84f, radius * 1.84f),
      style = Stroke(2.dp.toPx()),
    )
    val diamond =
      Path().apply {
        moveTo(center.x, center.y - radius * 0.45f)
        lineTo(center.x + radius * 0.35f, center.y)
        lineTo(center.x, center.y + radius * 0.45f)
        lineTo(center.x - radius * 0.35f, center.y)
        close()
      }
    drawPath(diamond, colors.primary, style = Stroke(2.dp.toPx()))
    drawCircle(colors.primary, radius * 0.07f)
  }
}
