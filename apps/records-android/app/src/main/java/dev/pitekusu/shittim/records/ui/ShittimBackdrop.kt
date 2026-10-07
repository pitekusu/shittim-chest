package dev.pitekusu.shittim.records.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.unit.dp

@Composable
fun ShittimBackdrop(modifier: Modifier = Modifier, content: @Composable BoxScope.() -> Unit) {
  val colors = MaterialTheme.colorScheme
  Surface(modifier.fillMaxSize(), color = colors.background, contentColor = colors.onBackground) {
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
}

@Composable
fun ShittimEmblem(modifier: Modifier = Modifier, ringRotation: Float = 0f) {
  val colors = MaterialTheme.colorScheme
  Canvas(modifier) {
    val side = size.minDimension
    val radius = side / 2f
    // Match the Web brand mark: halo, two circular borders, and a rotated square.
    drawCircle(colors.primary.copy(alpha = 0.12f), radius)
    drawCircle(colors.outlineVariant, radius * 0.94f, style = Stroke(1.5.dp.toPx()))
    drawCircle(colors.primary.copy(alpha = 0.58f), radius * 0.74f,
      style = Stroke(1.dp.toPx()))
    rotate(ringRotation) {
      drawArc(
        colors.primary,
        -80f,
        220f,
        false,
        topLeft = Offset(radius * 0.06f, radius * 0.06f),
        size = Size(radius * 1.88f, radius * 1.88f),
        style = Stroke(2.dp.toPx()),
      )
    }
    val squareSide = side * 0.38f
    val squareTopLeft = Offset(center.x - squareSide / 2f, center.y - squareSide / 2f)
    rotate(45f) {
      drawRect(colors.primary.copy(alpha = 0.12f), squareTopLeft, Size(squareSide, squareSide))
      drawRect(colors.primary, squareTopLeft, Size(squareSide, squareSide),
        style = Stroke(side * 0.085f))
    }
  }
}
