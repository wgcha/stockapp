package com.stockguide.app.data

import com.stockguide.app.BuildConfig
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL

class MobileApiClient(private val settings: ServerSettings) {
    suspend fun connectAndLoad(): MobileState = withContext(Dispatchers.IO) {
        val health = request("GET", "/health", authenticated = false)
        if (health.optString("status") != "ok" || health.optInt("api_version") != 1) {
            throw MobileApiException("모바일 API 버전이 맞지 않습니다.")
        }
        parseMobileState(request("GET", "/v1/state"))
    }

    suspend fun loadState(): MobileState = withContext(Dispatchers.IO) { parseMobileState(request("GET", "/v1/state")) }

    suspend fun saveProfile(amount: Double): MobileState = withContext(Dispatchers.IO) {
        parseMobileState(request("PUT", "/v1/profile", JSONObject().put("account_equity_krw", amount)))
    }

    suspend fun saveHolding(holding: Holding): MobileState = withContext(Dispatchers.IO) {
        val body = JSONObject().put("quantity", holding.quantity).put("average_price", holding.averagePrice)
        parseMobileState(request("PUT", "/v1/holdings/${holding.stockCode}", body))
    }

    suspend fun deleteHolding(code: String): MobileState = withContext(Dispatchers.IO) {
        parseMobileState(request("DELETE", "/v1/holdings/$code"))
    }

    suspend fun saveWatchlist(codes: List<String>): MobileState = withContext(Dispatchers.IO) {
        val symbols = JSONArray().apply { codes.distinct().forEach(::put) }
        parseMobileState(request("PUT", "/v1/watchlist", JSONObject().put("symbols", symbols)))
    }

    suspend fun getGuide(code: String): GuideResponse = withContext(Dispatchers.IO) {
        parseGuide(request("GET", "/v1/guides/$code"))
    }

    private fun request(method: String, path: String, body: JSONObject? = null, authenticated: Boolean = true): JSONObject {
        ServerAddressPolicy.validate(settings.baseUrl, BuildConfig.DEBUG)?.let { throw MobileApiException(it) }
        val connection = (URL(settings.baseUrl.trimEnd('/') + path).openConnection() as HttpURLConnection).apply {
            requestMethod = method
            connectTimeout = 8_000
            readTimeout = 8_000
            instanceFollowRedirects = false
            useCaches = false
            setRequestProperty("Accept", "application/json")
            setRequestProperty("Content-Type", "application/json; charset=utf-8")
            if (authenticated) setRequestProperty("Authorization", "Bearer ${settings.apiToken}")
            if (body != null) doOutput = true
        }
        try {
            if (body != null) {
                connection.outputStream.use { output -> output.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val payload = stream?.use(::readLimited).orEmpty()
            val json = runCatching { JSONObject(payload) }.getOrNull()
            if (status !in 200..299) {
                val message = json?.optJSONObject("error")?.optString("message")
                    ?.takeIf { !it.isNullOrBlank() } ?: "서버 요청에 실패했습니다. ($status)"
                throw MobileApiException(message)
            }
            return json ?: throw MobileApiException("서버 응답 형식이 올바르지 않습니다.")
        } catch (error: MobileApiException) {
            throw error
        } catch (_: Exception) {
            throw MobileApiException("서버에 연결하지 못했습니다. 주소와 네트워크 상태를 확인해 주세요.")
        } finally {
            connection.disconnect()
        }
    }

    private fun readLimited(input: InputStream): String {
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        var total = 0
        while (true) {
            val read = input.read(buffer)
            if (read < 0) break
            total += read
            if (total > MAX_RESPONSE_BYTES) throw MobileApiException("서버 응답 크기가 제한을 초과했습니다.")
            output.write(buffer, 0, read)
        }
        return output.toString(Charsets.UTF_8.name())
    }

    private companion object { const val MAX_RESPONSE_BYTES = 2 * 1024 * 1024 }
}
