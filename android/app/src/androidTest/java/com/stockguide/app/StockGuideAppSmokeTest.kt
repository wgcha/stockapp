package com.stockguide.app

import android.content.Context
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextReplacement
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class StockGuideAppSmokeTest {
    @get:Rule
    val compose = createAndroidComposeRule<MainActivity>()

    @Test
    fun demoTabsAndLocalInputsSurviveActivityRecreation() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val prefs = context.getSharedPreferences("demo_state_v1", Context.MODE_PRIVATE)
        val original = prefs.all.mapValues { it.value as? String }
        val serverPrefs = context.getSharedPreferences("server_settings_v1", Context.MODE_PRIVATE)
        val originalServer = serverPrefs.all.mapValues { it.value as? String }

        try {
            prefs.edit().clear().apply()
            serverPrefs.edit().putString("mode", "demo").apply()
            compose.activityRule.scenario.recreate()
            compose.waitForIdle()

            compose.onNodeWithText("오늘의 가이드").assertIsDisplayed()
            compose.onNodeWithText("서버 연결하기").performScrollTo().assertIsDisplayed()

            compose.onNodeWithTag("tab-holdings").performClick()
            compose.onNodeWithText("보유 · 관심종목").assertIsDisplayed()
            compose.onNodeWithTag("holding-stock-code").performScrollTo().performTextInput("005930")
            compose.onNodeWithTag("holding-quantity").performScrollTo().performTextInput("10")
            compose.onNodeWithTag("holding-average").performScrollTo().performTextInput("70000")
            compose.onNodeWithText("보유정보 저장").performScrollTo().performClick()
            compose.onNodeWithText("005930 · 삼성전자").assertIsDisplayed()

            compose.onNodeWithTag("tab-alerts").performClick()
            compose.onNodeWithText("평일 오전 9:10").assertIsDisplayed()

            compose.onNodeWithTag("tab-settings").performClick()
            compose.onNodeWithTag("account-equity").performScrollTo().performTextReplacement("10000000")
            compose.onNodeWithText("기준금액 저장").performScrollTo().performClick()

            compose.activityRule.scenario.recreate()
            compose.waitForIdle()
            compose.onNodeWithText("오늘의 가이드").assertIsDisplayed()
            compose.onNodeWithText("10,000,000원").assertIsDisplayed()
            compose.onNodeWithTag("tab-holdings").performClick()
            compose.onNodeWithText("005930 · 삼성전자").assertIsDisplayed()
        } finally {
            restoreStrings(prefs, original)
            restoreStrings(serverPrefs, originalServer)
            compose.activityRule.scenario.recreate()
        }
    }

    private fun restoreStrings(prefs: android.content.SharedPreferences, values: Map<String, String?>) {
        prefs.edit().clear().apply {
            values.forEach { (key, value) -> if (value != null) putString(key, value) }
        }.apply()
    }
}
