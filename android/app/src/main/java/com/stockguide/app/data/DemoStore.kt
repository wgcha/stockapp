package com.stockguide.app.data

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

class DemoStore(context: Context) {
    private val prefs = context.getSharedPreferences("demo_state_v1", Context.MODE_PRIVATE)

    fun load(): MobileState {
        val holdings = runCatching {
            val array = JSONArray(prefs.getString("holdings", "[]") ?: "[]")
            buildList {
                for (i in 0 until array.length()) {
                    val item = array.getJSONObject(i)
                    add(Holding(item.getString("stock_code"), item.getLong("quantity"), item.getDouble("average_price")))
                }
            }
        }.getOrDefault(emptyList())
        val watchlist = runCatching {
            val array = JSONArray(prefs.getString("watchlist", "[]") ?: "[]")
            buildList { for (i in 0 until array.length()) add(array.getString(i)) }
        }.getOrDefault(emptyList())
        val equity = prefs.getString("account_equity_krw", null)?.toDoubleOrNull()
            ?.takeIf { it.isFinite() && it in 1.0..1e15 }
        return MobileState(
            mode = AppMode.DEMO,
            accountEquityKrw = equity,
            holdings = holdings,
            watchlist = watchlist,
            marketStatus = "unconfigured",
            marketMessage = "데모 모드에서는 시장 자료를 조회하지 않습니다.",
        )
    }

    fun saveProfile(amount: Double) {
        prefs.edit().putString("account_equity_krw", amount.toLong().toString()).apply()
    }

    fun saveHolding(holding: Holding) {
        val items = load().holdings.filterNot { it.stockCode == holding.stockCode } + holding
        val array = JSONArray().apply {
            items.sortedBy(Holding::stockCode).forEach { item ->
                put(JSONObject().put("stock_code", item.stockCode).put("quantity", item.quantity).put("average_price", item.averagePrice))
            }
        }
        prefs.edit().putString("holdings", array.toString()).apply()
    }

    fun deleteHolding(code: String) {
        val array = JSONArray().apply {
            load().holdings.filterNot { it.stockCode == code }.sortedBy(Holding::stockCode).forEach { item ->
                put(JSONObject().put("stock_code", item.stockCode).put("quantity", item.quantity).put("average_price", item.averagePrice))
            }
        }
        prefs.edit().putString("holdings", array.toString()).apply()
    }

    fun saveWatchlist(codes: List<String>) {
        val array = JSONArray().apply { codes.distinct().forEach(::put) }
        prefs.edit().putString("watchlist", array.toString()).apply()
    }
}
