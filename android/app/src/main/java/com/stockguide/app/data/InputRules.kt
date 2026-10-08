package com.stockguide.app.data

object InputRules {
    fun stockCode(raw: String): String? = raw.trim().takeIf { it.matches(Regex("\\d{6}")) }

    fun accountAmount(raw: String): Double? = raw.replace(",", "").trim().toDoubleOrNull()
        ?.takeIf { it.isFinite() && it in 1.0..1e15 && it % 1.0 == 0.0 }

    fun quantity(raw: String): Long? = raw.trim().toLongOrNull()?.takeIf { it in 1L..1_000_000_000L }

    fun averagePrice(raw: String): Double? = raw.replace(",", "").trim().toDoubleOrNull()
        ?.takeIf { it.isFinite() && it in 1.0..1e12 }
}
