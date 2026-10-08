package com.stockguide.app.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ServerAddressPolicyTest {
    @Test fun debugAllowsLoopbackAndPrivateHttpOnly() {
        assertNull(ServerAddressPolicy.validate("http://127.0.0.1:8765", isDebug = true))
        assertNull(ServerAddressPolicy.validate("http://10.0.2.2:8765", isDebug = true))
        assertNull(ServerAddressPolicy.validate("http://192.168.1.20:8765", isDebug = true))
        assertEquals("디버그 HTTP는 localhost 또는 사설망 주소만 사용할 수 있습니다.", ServerAddressPolicy.validate("http://example.com", isDebug = true))
    }

    @Test fun releaseRequiresHttpsAndRejectsEmbeddedCredentials() {
        assertEquals("릴리스 앱은 HTTPS 서버만 연결할 수 있습니다.", ServerAddressPolicy.validate("http://192.168.1.20:8765", isDebug = false))
        assertNull(ServerAddressPolicy.validate("https://api.example.com", isDebug = false))
        assertEquals("서버 주소에 사용자 정보나 추가 매개변수를 넣을 수 없습니다.", ServerAddressPolicy.validate("https://user:pass@api.example.com", isDebug = false))
    }
}
