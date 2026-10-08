package com.stockguide.app.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class InputRulesTest {
    @Test fun stockCodesRequireSixDigits() {
        assertEquals("005930", InputRules.stockCode(" 005930 "))
        assertNull(InputRules.stockCode("5930"))
        assertNull(InputRules.stockCode("00593A"))
    }

    @Test fun amountsMustBeFinitePositiveAndIntegral() {
        assertEquals(10_000_000.0, InputRules.accountAmount("10,000,000")!!, 0.0)
        assertNull(InputRules.accountAmount("NaN"))
        assertNull(InputRules.accountAmount("1.5"))
        assertNull(InputRules.accountAmount("0"))
    }

    @Test fun holdingsRejectInvalidQuantityAndPrice() {
        assertEquals(10L, InputRules.quantity("10"))
        assertNull(InputRules.quantity("0"))
        assertNull(InputRules.quantity("1.2"))
        assertEquals(70_000.0, InputRules.averagePrice("70,000")!!, 0.0)
        assertNull(InputRules.averagePrice("Infinity"))
    }
}
