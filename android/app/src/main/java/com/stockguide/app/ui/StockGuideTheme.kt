package com.stockguide.app.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val LightColors = lightColorScheme(
    primary = Color(0xFF1B64DA),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFEDF3FF),
    onPrimaryContainer = Color(0xFF172F5A),
    secondary = Color(0xFF176B48),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFE7F6EF),
    onSecondaryContainer = Color(0xFF174A35),
    tertiary = Color(0xFF855D0C),
    onTertiary = Color.White,
    tertiaryContainer = Color(0xFFFFF3D8),
    onTertiaryContainer = Color(0xFF513900),
    background = Color(0xFFF2F4F6),
    onBackground = Color(0xFF191F28),
    surface = Color.White,
    onSurface = Color(0xFF191F28),
    surfaceVariant = Color(0xFFF3F5F7),
    onSurfaceVariant = Color(0xFF687381),
    outline = Color(0xFFE1E5E9),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFF79A9FF),
    onPrimary = Color(0xFF08254F),
    primaryContainer = Color(0xFF19345C),
    onPrimaryContainer = Color(0xFFD8E5FF),
    secondary = Color(0xFF8CD6B2),
    onSecondary = Color(0xFF123A29),
    secondaryContainer = Color(0xFF1D4634),
    onSecondaryContainer = Color(0xFFC1EBD4),
    tertiary = Color(0xFFE9C36D),
    onTertiary = Color(0xFF453300),
    tertiaryContainer = Color(0xFF594719),
    onTertiaryContainer = Color(0xFFF8E3A4),
    background = Color(0xFF121820),
    onBackground = Color(0xFFF1F4F8),
    surface = Color(0xFF1B242E),
    onSurface = Color(0xFFF1F4F8),
    surfaceVariant = Color(0xFF222D38),
    onSurfaceVariant = Color(0xFFACB6C2),
    outline = Color(0xFF34414E),
)

@Composable
fun StockGuideTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors, content = content)
}
