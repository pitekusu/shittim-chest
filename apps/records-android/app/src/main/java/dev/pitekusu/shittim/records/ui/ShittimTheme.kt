package dev.pitekusu.shittim.records.ui

import androidx.activity.compose.LocalActivity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialExpressiveTheme
import androidx.compose.material3.MotionScheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.view.WindowCompat
import dev.pitekusu.shittim.records.R

// Keep these roles aligned with Records Web; wallpaper colors must not replace the brand.
private val LightColors =
  lightColorScheme(
    primary = Color(0xFF1B7189),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFDFF8FB),
    onPrimaryContainer = Color(0xFF17324D),
    inversePrimary = Color(0xFF80E5F0),
    secondary = Color(0xFF72415B),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFFDE8F1),
    onSecondaryContainer = Color(0xFF45263A),
    tertiary = Color(0xFF635396),
    onTertiary = Color.White,
    tertiaryContainer = Color(0xFFEEECFF),
    onTertiaryContainer = Color(0xFF30264D),
    background = Color(0xFFF5FBFF),
    onBackground = Color(0xFF17324D),
    surface = Color(0xFFF5FBFF),
    onSurface = Color(0xFF17324D),
    surfaceTint = Color(0xFF1B7189),
    inverseSurface = Color(0xFF122C40),
    inverseOnSurface = Color(0xFFEAF8FF),
    surfaceBright = Color.White,
    surfaceDim = Color(0xFFDCECF3),
    surfaceContainerLowest = Color.White,
    surfaceContainer = Color.White,
    surfaceContainerHigh = Color(0xFFE8F4FA),
    surfaceContainerLow = Color(0xFFF8FCFF),
    surfaceContainerHighest = Color(0xFFDCECF3),
    onSurfaceVariant = Color(0xFF526A80),
    outline = Color(0xFF528899),
    outlineVariant = Color(0xFFB9DEE7),
    primaryFixed = Color(0xFFDFF8FB),
    primaryFixedDim = Color(0xFF80E5F0),
    onPrimaryFixed = Color(0xFF092D45),
    onPrimaryFixedVariant = Color(0xFF26556A),
    secondaryFixed = Color(0xFFFCE8F1),
    secondaryFixedDim = Color(0xFFF0A4C7),
    onSecondaryFixed = Color(0xFF45263A),
    onSecondaryFixedVariant = Color(0xFF633448),
    tertiaryFixed = Color(0xFFEEECFF),
    tertiaryFixedDim = Color(0xFFC7BAF4),
    onTertiaryFixed = Color(0xFF30264D),
    onTertiaryFixedVariant = Color(0xFF53427C),
  )

private val DarkColors =
  darkColorScheme(
    primary = Color(0xFF80E5F0),
    onPrimary = Color(0xFF092D45),
    primaryContainer = Color(0xFF123B49),
    onPrimaryContainer = Color(0xFFEAF8FF),
    inversePrimary = Color(0xFF1B7189),
    secondary = Color(0xFFF0A4C7),
    onSecondary = Color(0xFF45263A),
    secondaryContainer = Color(0xFF3C2739),
    onSecondaryContainer = Color(0xFFFCE8F1),
    tertiary = Color(0xFFC7BAF4),
    onTertiary = Color(0xFF30264D),
    tertiaryContainer = Color(0xFF2D2B4C),
    onTertiaryContainer = Color(0xFFEEECFF),
    background = Color(0xFF071724),
    onBackground = Color(0xFFEAF8FF),
    surface = Color(0xFF0B1F2F),
    onSurface = Color(0xFFEAF8FF),
    surfaceTint = Color(0xFF80E5F0),
    inverseSurface = Color(0xFFEAF8FF),
    inverseOnSurface = Color(0xFF17324D),
    surfaceBright = Color(0xFF244355),
    surfaceDim = Color(0xFF071724),
    surfaceContainerLowest = Color(0xFF071724),
    surfaceContainer = Color(0xFF122C40),
    surfaceContainerHigh = Color(0xFF19374A),
    surfaceContainerLow = Color(0xFF0C2434),
    surfaceContainerHighest = Color(0xFF244355),
    onSurfaceVariant = Color(0xFFA8C5D4),
    outline = Color(0xFF68AAB9),
    outlineVariant = Color(0xFF28546A),
    primaryFixed = Color(0xFFDFF8FB),
    primaryFixedDim = Color(0xFF80E5F0),
    onPrimaryFixed = Color(0xFF092D45),
    onPrimaryFixedVariant = Color(0xFF26556A),
    secondaryFixed = Color(0xFFFCE8F1),
    secondaryFixedDim = Color(0xFFF0A4C7),
    onSecondaryFixed = Color(0xFF45263A),
    onSecondaryFixedVariant = Color(0xFF633448),
    tertiaryFixed = Color(0xFFEEECFF),
    tertiaryFixedDim = Color(0xFFC7BAF4),
    onTertiaryFixed = Color(0xFF30264D),
    onTertiaryFixedVariant = Color(0xFF53427C),
  )

private val LineSeed =
  FontFamily(
    Font(R.font.line_seed_jp_regular, FontWeight.Normal),
    Font(R.font.line_seed_jp_bold, FontWeight.Bold),
  )
val ShittimDisplayFont = FontFamily(Font(R.font.delogy_regular))
private val ShittimTypography = Typography(fontFamily = LineSeed)
private val ShittimShapes =
  Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(12.dp),
    medium = RoundedCornerShape(16.dp),
    large = RoundedCornerShape(24.dp),
    extraLarge = RoundedCornerShape(32.dp),
  )

@Composable
fun ShittimTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
  val window = LocalActivity.current?.window
  val view = LocalView.current
  SideEffect {
    if (window != null) {
      WindowCompat.getInsetsController(window, view).apply {
        isAppearanceLightStatusBars = !darkTheme
        isAppearanceLightNavigationBars = !darkTheme
      }
    }
  }
  MaterialExpressiveTheme(
    colorScheme = if (darkTheme) DarkColors else LightColors,
    typography = ShittimTypography,
    shapes = ShittimShapes,
    motionScheme = MotionScheme.expressive(),
    content = content,
  )
}
