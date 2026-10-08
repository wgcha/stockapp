package com.stockguide.app

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import com.stockguide.app.ui.StockGuideApp
import com.stockguide.app.ui.StockGuideTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            StockGuideTheme {
                StockGuideApp()
            }
        }
    }
}
