package com.stockguide.app.data

import org.json.JSONObject
import java.time.OffsetDateTime
import java.util.Locale

enum class AppMode { DEMO, SERVER }

data class Holding(
    val stockCode: String,
    val quantity: Long,
    val averagePrice: Double,
)

data class MobileState(
    val mode: AppMode,
    val accountEquityKrw: Double?,
    val holdings: List<Holding>,
    val watchlist: List<String>,
    val marketStatus: String,
    val marketMessage: String,
) {
    companion object {
        fun emptyServer() = MobileState(
            mode = AppMode.SERVER,
            accountEquityKrw = null,
            holdings = emptyList(),
            watchlist = emptyList(),
            marketStatus = "unconfigured",
            marketMessage = "서버에서 자료를 불러오지 못했습니다.",
        )
    }
}

data class GuideResponse(
    val stockCode: String,
    val action: String,
    val label: String,
    val confidence: Double,
    val text: String,
    val currentPrice: Double?,
    val priceAsOf: String?,
    val generatedAt: String?,
    val marketStatus: String,
    val isDemo: Boolean,
)

data class ServerSettings(val baseUrl: String, val apiToken: String)

class MobileApiException(val userMessage: String) : Exception(userMessage)

fun parseMobileState(json: JSONObject): MobileState {
    if (json.optString("mode") != "server" || json.optBoolean("guide_only", false).not()) {
        throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
    }

    val equityValue = json.opt("account_equity_krw")
    val equity = when (equityValue) {
        null, JSONObject.NULL -> null
        is Number -> equityValue.toDouble().also {
            if (!it.isFinite() || it <= 0.0) throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
        }
        else -> throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
    }

    val holdingArray = json.optJSONArray("holdings")
        ?: throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
    val holdings = buildList {
        for (i in 0 until holdingArray.length()) {
            val item = holdingArray.optJSONObject(i)
                ?: throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
            val code = item.optString("stock_code")
            val quantityValue = item.opt("quantity")
            val priceValue = item.opt("average_price")
            val quantity = (quantityValue as? Number)?.toLong()
            val price = (priceValue as? Number)?.toDouble()
            if (!code.matches(Regex("\\d{6}")) || quantity == null || quantity !in 1..1_000_000_000L ||
                price == null || !price.isFinite() || price <= 0.0 || price > 1e12
            ) {
                throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
            }
            add(Holding(code, quantity, price))
        }
    }

    val watchlistArray = json.optJSONArray("watchlist")
        ?: throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
    val watchlist = buildList {
        for (i in 0 until watchlistArray.length()) {
            val code = watchlistArray.optString(i)
            if (!code.matches(Regex("\\d{6}"))) throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
            add(code)
        }
    }

    val market = json.optJSONObject("market")
        ?: throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
    val marketStatus = market.optString("status")
    if (marketStatus !in setOf("unconfigured", "configured", "error")) {
        throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
    }
    return MobileState(
        mode = AppMode.SERVER,
        accountEquityKrw = equity,
        holdings = holdings,
        watchlist = watchlist,
        marketStatus = marketStatus,
        marketMessage = market.optString("message", "시세 상태를 확인할 수 없습니다."),
    )
}

fun parseGuide(json: JSONObject): GuideResponse {
    val code = json.optString("stock_code")
    val action = json.optString("action")
    val label = json.optString("label")
    val confidence = (json.opt("confidence") as? Number)?.toDouble()
        ?: throw MobileApiException("가이드 응답 형식이 올바르지 않습니다.")
    val text = json.optString("text")
    val marketStatus = json.optString("market_status")
    val priceValue = json.opt("current_price")
    val price = when (priceValue) {
        null, JSONObject.NULL -> null
        is Number -> priceValue.toDouble().also {
            if (!it.isFinite() || it <= 0.0) throw MobileApiException("가이드 응답 형식이 올바르지 않습니다.")
        }
        else -> throw MobileApiException("가이드 응답 형식이 올바르지 않습니다.")
    }
    if (!code.matches(Regex("\\d{6}")) || label.isBlank() || text.isBlank() ||
        !confidence.isFinite() || confidence !in 0.0..1.0 || marketStatus !in setOf("unconfigured", "configured", "error")
    ) {
        throw MobileApiException("가이드 응답 형식이 올바르지 않습니다.")
    }
    val priceAsOf = nullableText(json, "price_as_of", requireIsoTimestamp = true)
    if (price != null && priceAsOf == null) throw MobileApiException("가이드 응답 형식이 올바르지 않습니다.")
    return GuideResponse(
        stockCode = code,
        action = action,
        label = label,
        confidence = confidence,
        text = text,
        currentPrice = price,
        priceAsOf = priceAsOf,
        generatedAt = nullableText(json, "generated_at", requireIsoTimestamp = true),
        marketStatus = marketStatus,
        isDemo = json.optBoolean("is_demo", false),
    )
}

private fun nullableText(json: JSONObject, key: String, requireIsoTimestamp: Boolean): String? {
    val raw = json.opt(key)
    if (raw == null || raw === JSONObject.NULL) return null
    if (raw !is String) throw MobileApiException("가이드 응답 형식이 올바르지 않습니다.")
    val value = raw.trim().takeIf(String::isNotEmpty) ?: return null
    if (requireIsoTimestamp && runCatching { OffsetDateTime.parse(value) }.isFailure) {
        throw MobileApiException("가이드 응답 형식이 올바르지 않습니다.")
    }
    return value
}

fun formatKrw(amount: Double?): String = amount?.takeIf { it.isFinite() && it > 0 }
    ?.let { String.format(Locale.KOREA, "%,.0f원", it) } ?: "미설정"
