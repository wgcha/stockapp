package com.stockguide.app.data

import java.net.URI

object ServerAddressPolicy {
    fun validate(baseUrl: String, isDebug: Boolean): String? {
        val uri = runCatching { URI(baseUrl.trim()) }.getOrNull() ?: return "서버 주소 형식을 확인해 주세요."
        val scheme = uri.scheme?.lowercase() ?: return "서버 주소는 https://로 시작해야 합니다."
        val host = uri.host?.trim('[', ']')?.lowercase() ?: return "서버 주소에 호스트가 필요합니다."
        if (uri.userInfo != null || uri.query != null || uri.fragment != null) return "서버 주소에 사용자 정보나 추가 매개변수를 넣을 수 없습니다."
        if (scheme == "https") return null
        if (!isDebug) return "릴리스 앱은 HTTPS 서버만 연결할 수 있습니다."
        if (scheme != "http" || !isDebugHttpHostAllowed(host)) {
            return "디버그 HTTP는 localhost 또는 사설망 주소만 사용할 수 있습니다."
        }
        return null
    }

    fun isDebugHttpHostAllowed(host: String): Boolean {
        val normalized = host.trim('[', ']')
        if (normalized.equals("localhost", ignoreCase = true) || normalized == "::1" || normalized == "10.0.2.2") return true
        val parts = normalized.split('.')
        if (parts.size != 4) return false
        val octets = parts.map { it.toIntOrNull() ?: return false }
        if (octets.any { it !in 0..255 }) return false
        return octets[0] == 10 ||
            octets[0] == 127 ||
            (octets[0] == 172 && octets[1] in 16..31) ||
            (octets[0] == 192 && octets[1] == 168
        )
    }
}
