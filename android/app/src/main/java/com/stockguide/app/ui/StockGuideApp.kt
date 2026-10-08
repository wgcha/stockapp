package com.stockguide.app.ui

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.DeleteOutline
import androidx.compose.material.icons.outlined.Home
import androidx.compose.material.icons.outlined.Inventory2
import androidx.compose.material.icons.outlined.NotificationsNone
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material.icons.outlined.StarBorder
import androidx.compose.material.icons.outlined.WifiOff
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import com.stockguide.app.BuildConfig
import com.stockguide.app.data.AppMode
import com.stockguide.app.data.DemoStore
import com.stockguide.app.data.GuideResponse
import com.stockguide.app.data.Holding
import com.stockguide.app.data.InputRules
import com.stockguide.app.data.MobileApiClient
import com.stockguide.app.data.MobileApiException
import com.stockguide.app.data.MobileState
import com.stockguide.app.data.ServerAddressPolicy
import com.stockguide.app.data.ServerSettings
import com.stockguide.app.data.ServerSettingsStore
import com.stockguide.app.data.formatKrw
import com.stockguide.app.reminder.ReminderReceiver
import com.stockguide.app.reminder.ReminderScheduler
import java.text.NumberFormat
import java.util.Locale
import kotlinx.coroutines.launch

private enum class AppTab(val label: String, val icon: ImageVector) {
    TODAY("오늘", Icons.Outlined.Home),
    HOLDINGS("보유·관심", Icons.Outlined.Inventory2),
    ALERTS("알림", Icons.Outlined.NotificationsNone),
    SETTINGS("설정", Icons.Outlined.Settings),
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun StockGuideApp() {
    val context = LocalContext.current
    val demoStore = remember { DemoStore(context) }
    val settingsStore = remember { ServerSettingsStore(context) }
    val reminderPrefs = remember { context.getSharedPreferences(ReminderReceiver.PREFERENCES, Context.MODE_PRIVATE) }
    val initialMode = remember { settingsStore.mode() }
    var mode by remember { mutableStateOf(initialMode) }
    var data by remember { mutableStateOf(if (initialMode == AppMode.DEMO) demoStore.load() else MobileState.emptyServer()) }
    var currentTab by remember { mutableStateOf(AppTab.TODAY) }
    var appMessage by remember { mutableStateOf(if (initialMode == AppMode.DEMO) "투자정보를 입력해 화면을 먼저 살펴보세요." else "서버 상태를 확인하고 있습니다.") }
    var serverOk by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var activeGuide by remember { mutableStateOf<GuideResponse?>(null) }
    var reminderOn by remember { mutableStateOf(reminderPrefs.getBoolean(ReminderReceiver.KEY_ENABLED, false)) }
    var capitalInput by remember { mutableStateOf(data.accountEquityKrw?.let(::plainNumber).orEmpty()) }
    var holdingCode by remember { mutableStateOf("") }
    var holdingQuantity by remember { mutableStateOf("") }
    var holdingAverage by remember { mutableStateOf("") }
    var watchCode by remember { mutableStateOf("") }
    var guideCode by remember { mutableStateOf("") }
    var serverUrl by remember { mutableStateOf(settingsStore.savedBaseUrl()) }
    var apiToken by remember { mutableStateOf("") }
    var savedTokenAvailable by remember { mutableStateOf(settingsStore.loadSettings() != null) }
    val scope = rememberCoroutineScope()

    fun showError(error: Throwable) {
        serverOk = false
        appMessage = (error as? MobileApiException)?.userMessage ?: "요청을 완료하지 못했습니다. 입력값과 연결 상태를 확인해 주세요."
    }

    fun runServerMutation(
        successMessage: String,
        action: suspend (MobileApiClient) -> MobileState,
        afterSuccess: () -> Unit = {},
    ) {
        val settings = settingsStore.loadSettings()
        if (settings == null) {
            appMessage = "설정 탭에서 서버 주소와 개인 API 키를 저장해 주세요."
            return
        }
        scope.launch {
            busy = true
            try {
                val updated = action(MobileApiClient(settings))
                data = updated
                capitalInput = updated.accountEquityKrw?.let(::plainNumber).orEmpty()
                mode = AppMode.SERVER
                settingsStore.setMode(AppMode.SERVER)
                serverOk = true
                appMessage = successMessage
                afterSuccess()
            } catch (error: Throwable) {
                showError(error)
            } finally {
                busy = false
            }
        }
    }

    fun checkSavedServer() {
        val settings = settingsStore.loadSettings()
        if (settings == null) {
            appMessage = "서버 모드입니다. 설정 탭에서 주소와 개인 API 키를 입력해 연결해 주세요."
            serverOk = false
            return
        }
        scope.launch {
            busy = true
            try {
                data = MobileApiClient(settings).connectAndLoad()
                capitalInput = data.accountEquityKrw?.let(::plainNumber).orEmpty()
                serverOk = true
                appMessage = "서버 자료를 불러왔습니다. 설정됨은 최신 시세 확인을 뜻하지 않습니다."
            } catch (error: Throwable) {
                showError(error)
            } finally {
                busy = false
            }
        }
    }

    fun connectToServer() {
        val normalizedUrl = serverUrl.trim().trimEnd('/')
        val addressError = ServerAddressPolicy.validate(normalizedUrl, BuildConfig.DEBUG)
        if (addressError != null) {
            appMessage = addressError
            return
        }
        val previous = settingsStore.loadSettings()
        val token = apiToken.trim().ifBlank {
            previous?.takeIf { it.baseUrl.trimEnd('/') == normalizedUrl }?.apiToken.orEmpty()
        }
        if (token.length < 32) {
            appMessage = "개인 API 키를 입력해 주세요. 서버 키는 32자 이상이어야 합니다."
            return
        }
        try {
            val settings = ServerSettings(normalizedUrl, token)
            settingsStore.saveSettings(settings)
            settingsStore.setMode(AppMode.SERVER)
            savedTokenAvailable = true
            apiToken = ""
            mode = AppMode.SERVER
            data = MobileState.emptyServer()
            capitalInput = ""
            holdingCode = ""
            holdingQuantity = ""
            holdingAverage = ""
            watchCode = ""
            guideCode = ""
            activeGuide = null
            currentTab = AppTab.TODAY
            appMessage = "서버 모드로 연결 중입니다. 실패하면 데모로 바꾸지 않고 오류를 표시합니다."
            scope.launch {
                busy = true
                serverOk = false
                try {
                    data = MobileApiClient(settings).connectAndLoad()
                    capitalInput = data.accountEquityKrw?.let(::plainNumber).orEmpty()
                    serverOk = true
                    appMessage = "서버에 연결했습니다. 시세 상태는 별도로 확인해 주세요."
                } catch (error: Throwable) {
                    showError(error)
                } finally {
                    busy = false
                }
            }
        } catch (_: Exception) {
            appMessage = "서버 키를 안전하게 저장하지 못했습니다. 기기 보안 상태를 확인해 주세요."
        }
    }

    fun switchToDemo() {
        settingsStore.setMode(AppMode.DEMO)
        mode = AppMode.DEMO
        data = demoStore.load()
        activeGuide = null
        serverOk = false
        currentTab = AppTab.TODAY
        capitalInput = data.accountEquityKrw?.let(::plainNumber).orEmpty()
        holdingCode = ""
        holdingQuantity = ""
        holdingAverage = ""
        watchCode = ""
        guideCode = ""
        appMessage = "오프라인 데모 모드입니다. 서버 자료와 별도 저장 공간을 사용합니다."
    }

    fun saveCapital() {
        val amount = InputRules.accountAmount(capitalInput)
        if (amount == null) {
            appMessage = "투자 기준금액은 1원 이상 1,000조원 이하의 정수로 입력해 주세요."
            return
        }
        if (mode == AppMode.DEMO) {
            demoStore.saveProfile(amount)
            data = data.copy(accountEquityKrw = amount)
            appMessage = "데모 투자 기준금액을 이 기기에 저장했습니다."
        } else {
            runServerMutation("서버에 투자 기준금액을 저장했습니다.", { it.saveProfile(amount) })
        }
    }

    fun saveHolding() {
        val code = InputRules.stockCode(holdingCode)
        val quantity = InputRules.quantity(holdingQuantity)
        val average = InputRules.averagePrice(holdingAverage)
        if (code == null || quantity == null || average == null) {
            appMessage = "종목코드 6자리, 수량 1 이상, 평균매입가 1원 이상을 확인해 주세요."
            return
        }
        val holding = Holding(code, quantity, average)
        if (mode == AppMode.DEMO) {
            demoStore.saveHolding(holding)
            data = demoStore.load()
            appMessage = "데모 보유정보를 이 기기에 저장했습니다."
            holdingCode = ""
            holdingQuantity = ""
            holdingAverage = ""
        } else {
            runServerMutation("서버에 보유정보를 저장했습니다.", { it.saveHolding(holding) }) {
                holdingCode = ""
                holdingQuantity = ""
                holdingAverage = ""
            }
        }
    }

    fun deleteHolding(code: String) {
        if (mode == AppMode.DEMO) {
            demoStore.deleteHolding(code)
            data = demoStore.load()
            appMessage = "데모 보유정보를 삭제했습니다."
        } else {
            runServerMutation("서버에서 보유정보를 삭제했습니다.", { it.deleteHolding(code) })
        }
    }

    fun addWatchCode() {
        val code = InputRules.stockCode(watchCode)
        if (code == null) {
            appMessage = "관심 종목 코드는 숫자 6자리로 입력해 주세요."
            return
        }
        if (code in data.watchlist) {
            appMessage = "이미 관심종목에 있습니다."
            return
        }
        if (data.watchlist.size >= 50) {
            appMessage = "관심종목은 최대 50개까지 저장할 수 있습니다."
            return
        }
        val next = data.watchlist + code
        if (mode == AppMode.DEMO) {
            demoStore.saveWatchlist(next)
            data = demoStore.load()
            appMessage = "데모 관심종목을 이 기기에 저장했습니다."
            watchCode = ""
        } else {
            runServerMutation("서버에 관심종목을 저장했습니다.", { it.saveWatchlist(next) }) { watchCode = "" }
        }
    }

    fun removeWatchCode(code: String) {
        val next = data.watchlist.filterNot { it == code }
        if (mode == AppMode.DEMO) {
            demoStore.saveWatchlist(next)
            data = demoStore.load()
            appMessage = "데모 관심종목을 삭제했습니다."
        } else {
            runServerMutation("서버에서 관심종목을 삭제했습니다.", { it.saveWatchlist(next) })
        }
    }

    fun fetchGuide() {
        activeGuide = null
        val code = InputRules.stockCode(guideCode)
        if (code == null) {
            appMessage = "가이드를 확인할 종목 코드를 숫자 6자리로 입력해 주세요."
            return
        }
        if (mode == AppMode.DEMO) {
            activeGuide = null
            appMessage = "데모 모드에서는 실제 시세와 가이드를 조회하지 않습니다. 서버 연결 후 다시 시도해 주세요."
            return
        }
        val settings = settingsStore.loadSettings()
        if (settings == null) {
            appMessage = "서버 주소와 개인 API 키를 먼저 설정해 주세요."
            return
        }
        scope.launch {
            busy = true
            try {
                activeGuide = MobileApiClient(settings).getGuide(code)
                serverOk = true
                appMessage = "서버에서 가이드를 받았습니다. 실시간 주문 기능은 없습니다."
            } catch (error: Throwable) {
                activeGuide = null
                showError(error)
            } finally {
                busy = false
            }
        }
    }

    fun toggleReminder(enable: Boolean, granted: Boolean) {
        if (!enable) {
            reminderPrefs.edit().putBoolean(ReminderReceiver.KEY_ENABLED, false).apply()
            ReminderScheduler(context).cancel()
            reminderOn = false
            appMessage = "기기 리마인더를 껐습니다."
            return
        }
        if (!granted) {
            appMessage = "기기 알림 권한을 허용하면 평일 확인 리마인더를 켤 수 있습니다."
            return
        }
        reminderPrefs.edit().putBoolean(ReminderReceiver.KEY_ENABLED, true).apply()
        ReminderScheduler(context).scheduleNext()
        reminderOn = true
        appMessage = "평일 09:10 기기 확인 리마인더를 켰습니다. 실시간 투자 신호가 아닙니다."
    }

    val notificationPermissionLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) toggleReminder(true, true) else appMessage = "알림 권한이 없어 리마인더를 켜지 않았습니다. 설정에서 권한을 허용할 수 있습니다."
    }

    LaunchedEffect(Unit) {
        if (initialMode == AppMode.SERVER) checkSavedServer()
        if (reminderOn) ReminderScheduler(context).scheduleNext()
    }

    val marketLabel = when (data.marketStatus) {
        "configured" -> "시세 설정 있음 · 조회 성공과 다름"
        "error" -> "시세 조회 오류"
        else -> "시세 정보 미설정"
    }

    Scaffold(
        contentWindowInsets = WindowInsets.safeDrawing,
        bottomBar = {
            NavigationBar(containerColor = MaterialTheme.colorScheme.surface) {
                AppTab.entries.forEach { tab ->
                    NavigationBarItem(
                        selected = currentTab == tab,
                        onClick = { currentTab = tab },
                        icon = { Icon(tab.icon, contentDescription = null) },
                        label = { Text(tab.label, maxLines = 1) },
                        modifier = Modifier.testTag("tab-${tab.name.lowercase(Locale.ROOT)}"),
                    )
                }
            }
        },
    ) { innerPadding ->
        Column(
            modifier = Modifier.fillMaxSize().padding(innerPadding).imePadding().background(MaterialTheme.colorScheme.background),
        ) {
            AppHeader(mode = mode, serverOk = serverOk, busy = busy)
            if (appMessage.isNotBlank()) MessageBanner(appMessage)
            Box(modifier = Modifier.fillMaxWidth().weight(1f)) {
                when (currentTab) {
                AppTab.TODAY -> TodayScreen(
                    mode = mode,
                    data = data,
                    marketLabel = marketLabel,
                    guideCode = guideCode,
                    guide = activeGuide,
                    busy = busy,
                    onGuideCodeChange = { guideCode = it },
                    onFetchGuide = ::fetchGuide,
                    onOpenSettings = { currentTab = AppTab.SETTINGS },
                )
                AppTab.HOLDINGS -> HoldingsScreen(
                    data = data,
                    code = holdingCode,
                    quantity = holdingQuantity,
                    average = holdingAverage,
                    watchCode = watchCode,
                    onCodeChange = { holdingCode = it },
                    onQuantityChange = { holdingQuantity = it },
                    onAverageChange = { holdingAverage = it },
                    onWatchCodeChange = { watchCode = it },
                    onSaveHolding = ::saveHolding,
                    onDeleteHolding = ::deleteHolding,
                    onAddWatchCode = ::addWatchCode,
                    onRemoveWatchCode = ::removeWatchCode,
                    busy = busy,
                )
                AppTab.ALERTS -> AlertsScreen(
                    reminderOn = reminderOn,
                    busy = busy,
                    onToggle = { enable ->
                        if (!enable) toggleReminder(false, true)
                        else if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
                        } else toggleReminder(true, true)
                    },
                )
                AppTab.SETTINGS -> SettingsScreen(
                    mode = mode,
                    serverOk = serverOk,
                    marketLabel = marketLabel,
                    capitalInput = capitalInput,
                    serverUrl = serverUrl,
                    apiToken = apiToken,
                    savedTokenAvailable = savedTokenAvailable,
                    busy = busy,
                    onCapitalChange = { capitalInput = it },
                    onSaveCapital = ::saveCapital,
                    onUrlChange = { serverUrl = it },
                    onTokenChange = { apiToken = it },
                    onConnect = ::connectToServer,
                    onRefresh = ::checkSavedServer,
                    onSwitchToDemo = ::switchToDemo,
                    onSwitchToAlerts = { currentTab = AppTab.ALERTS },
                )
                }
            }
        }
    }
}

@Composable
private fun AppHeader(mode: AppMode, serverOk: Boolean, busy: Boolean) {
    Row(
        modifier = Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface).padding(horizontal = 20.dp, vertical = 15.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Row(Modifier.weight(1f), verticalAlignment = Alignment.CenterVertically) {
            Surface(shape = RoundedCornerShape(12.dp), color = MaterialTheme.colorScheme.primaryContainer) {
                Text("₩", modifier = Modifier.padding(horizontal = 11.dp, vertical = 7.dp), color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold, fontSize = 18.sp)
            }
            Spacer(Modifier.width(10.dp))
            Column(Modifier.weight(1f)) {
                Text("주식 가이드", fontWeight = FontWeight.Bold, fontSize = 16.sp)
                Text("가이드 전용 · 주문 없음", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, maxLines = 1, softWrap = false, overflow = TextOverflow.Ellipsis)
            }
        }
        val label = when {
            mode == AppMode.DEMO -> "데모"
            busy -> "확인 중"
            serverOk -> "연결됨"
            else -> "서버 모드"
        }
        Surface(shape = CircleShape, color = if (mode == AppMode.DEMO) MaterialTheme.colorScheme.tertiaryContainer else MaterialTheme.colorScheme.surfaceVariant) {
            Text(label, modifier = Modifier.padding(horizontal = 9.dp, vertical = 7.dp), color = if (mode == AppMode.DEMO) MaterialTheme.colorScheme.onTertiaryContainer else MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 11.sp, fontWeight = FontWeight.SemiBold, maxLines = 1, softWrap = false)
        }
    }
}

@Composable
private fun MessageBanner(message: String) {
    Surface(color = MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.65f)) {
        Text(message, modifier = Modifier.fillMaxWidth().padding(horizontal = 18.dp, vertical = 10.dp), color = MaterialTheme.colorScheme.onPrimaryContainer, fontSize = 12.sp, lineHeight = 17.sp)
    }
}

@Composable
private fun ScreenColumn(content: @Composable ColumnScope.() -> Unit) {
    Column(
        modifier = Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 18.dp, vertical = 16.dp),
        verticalArrangement = Arrangement.spacedBy(13.dp),
        content = content,
    )
}

@Composable
private fun TodayScreen(
    mode: AppMode,
    data: MobileState,
    marketLabel: String,
    guideCode: String,
    guide: GuideResponse?,
    busy: Boolean,
    onGuideCodeChange: (String) -> Unit,
    onFetchGuide: () -> Unit,
    onOpenSettings: () -> Unit,
) {
    ScreenColumn {
        Column {
            Text("오늘의 가이드", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(if (mode == AppMode.DEMO) "오프라인에서 입력 흐름을 확인해요." else "서버 자료와 시세 연결 상태를 확인해요.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(17.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Outlined.Inventory2, contentDescription = null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.width(7.dp))
                    Text("투자 기준금액", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp, fontWeight = FontWeight.Medium)
                }
                Spacer(Modifier.height(4.dp))
                Text(formatKrw(data.accountEquityKrw), fontSize = 30.sp, fontWeight = FontWeight.Bold, letterSpacing = (-0.7).sp)
                Text(if (data.accountEquityKrw == null) "설정에서 금액을 입력해 주세요 · 잔고가 아닙니다" else "예산 계산 기준 · 계좌 잔고나 평가금액이 아닙니다", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
            }
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
                    StatusPill(if (mode == AppMode.DEMO) "데모 자료" else "서버 자료", if (mode == AppMode.DEMO) MaterialTheme.colorScheme.tertiaryContainer else MaterialTheme.colorScheme.primaryContainer)
                    StatusPill(marketLabel, MaterialTheme.colorScheme.surfaceVariant)
                }
                Text("종목 가이드", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.Bold)
                if (data.holdings.isEmpty() && data.watchlist.isEmpty()) {
                    Text("보유 또는 관심 종목을 추가하면 종목별로 확인할 수 있어요.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
                } else {
                    (data.holdings.map { it.stockCode } + data.watchlist).distinct().take(4).forEach { code ->
                        StockGuideRow(code = code, mode = mode, onSelect = onGuideCodeChange)
                    }
                }
                OutlinedTextField(
                    value = guideCode,
                    onValueChange = { onGuideCodeChange(it.filter(Char::isDigit).take(6)) },
                    label = { Text("가이드 종목코드") },
                    placeholder = { Text("예: 005930") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth(),
                )
                Button(
                    onClick = if (mode == AppMode.DEMO) onOpenSettings else onFetchGuide,
                    enabled = !busy && (mode == AppMode.DEMO || guideCode.length == 6),
                    modifier = Modifier.fillMaxWidth().height(48.dp),
                ) {
                    if (busy) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
                    else Text(if (mode == AppMode.DEMO) "서버 연결하기" else "가이드 확인")
                }
                if (mode == AppMode.DEMO) {
                    Text("데모 모드는 시세를 조회하지 않으며 실제 가격·손익을 표시하지 않습니다.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, lineHeight = 17.sp)
                }
                if (guide != null) GuideResultCard(guide)
            }
        }
        if (mode == AppMode.SERVER && data.marketStatus == "unconfigured") {
            OutlinedButton(onClick = onOpenSettings, modifier = Modifier.fillMaxWidth().height(48.dp)) {
                Icon(Icons.Outlined.Settings, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(8.dp))
                Text("시세 연결 설정 보기")
            }
        }
    }
}

@Composable
private fun StockGuideRow(code: String, mode: AppMode, onSelect: (String) -> Unit) {
    val holdingName = stockName(code)
    Card(
        onClick = { onSelect(code) },
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.55f)),
        shape = RoundedCornerShape(13.dp),
    ) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 13.dp, vertical = 11.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("$code · $holdingName", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                Text(if (mode == AppMode.DEMO) "데모 · 시세 미조회" else "시세 연결 상태는 별도 확인", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
            }
            Text("확인", color = MaterialTheme.colorScheme.primary, fontSize = 12.sp, fontWeight = FontWeight.SemiBold)
        }
    }
}

@Composable
private fun GuideResultCard(guide: GuideResponse) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer.copy(alpha = 0.55f))) {
        Column(Modifier.fillMaxWidth().padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween, verticalAlignment = Alignment.CenterVertically) {
                Text("${guide.stockCode} · ${stockName(guide.stockCode)}", fontWeight = FontWeight.Bold)
                Text(guide.label, color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold, fontSize = 13.sp)
            }
            Text("신뢰도 ${String.format(Locale.KOREA, "%.0f", guide.confidence * 100)}%", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            guide.currentPrice?.let { price ->
                Text("조회된 현재가 ${formatKrw(price)}", fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                guide.priceAsOf?.let { Text("가격 기준 시각 · $it", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp) }
            }
            if (guide.isDemo) Text("서버가 데모 응답으로 표시했습니다.", color = MaterialTheme.colorScheme.tertiary, fontSize = 12.sp)
            Text(guide.text, fontSize = 13.sp, lineHeight = 19.sp)
            guide.generatedAt?.let { Text("생성 시각 · $it", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 11.sp) }
        }
    }
}

@Composable
private fun HoldingsScreen(
    data: MobileState,
    code: String,
    quantity: String,
    average: String,
    watchCode: String,
    onCodeChange: (String) -> Unit,
    onQuantityChange: (String) -> Unit,
    onAverageChange: (String) -> Unit,
    onWatchCodeChange: (String) -> Unit,
    onSaveHolding: () -> Unit,
    onDeleteHolding: (String) -> Unit,
    onAddWatchCode: () -> Unit,
    onRemoveWatchCode: (String) -> Unit,
    busy: Boolean,
) {
    ScreenColumn {
        Column {
            Text("보유 · 관심종목", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text(if (data.mode == AppMode.DEMO) "이 기기에만 저장되는 오프라인 데모 정보입니다." else "서버에 저장된 보유와 관심종목입니다.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
        }
        SectionTitle("보유종목", "현재가·평가손익은 실제 조회 결과가 있을 때만 표시합니다.")
        if (data.holdings.isEmpty()) EmptyCard("등록된 보유종목이 없습니다.")
        data.holdings.forEach { holding ->
            HoldingCard(holding = holding, demo = data.mode == AppMode.DEMO, onDelete = { onDeleteHolding(holding.stockCode) })
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(15.dp), verticalArrangement = Arrangement.spacedBy(9.dp)) {
                Text("보유 추가 또는 수정", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                OutlinedTextField(code, { onCodeChange(it.filter(Char::isDigit).take(6)) }, label = { Text("종목코드 6자리") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.fillMaxWidth().testTag("holding-stock-code"))
                Row(horizontalArrangement = Arrangement.spacedBy(9.dp)) {
                    OutlinedTextField(quantity, { onQuantityChange(it.filter(Char::isDigit).take(10)) }, label = { Text("수량") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.weight(1f).testTag("holding-quantity"))
                    OutlinedTextField(average, { onAverageChange(it.filter { c -> c.isDigit() || c == '.' }.take(16)) }, label = { Text("평균매입가") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal), modifier = Modifier.weight(1f).testTag("holding-average"))
                }
                Button(onClick = onSaveHolding, enabled = !busy, modifier = Modifier.fillMaxWidth().height(48.dp)) { Text("보유정보 저장") }
            }
        }
        SectionTitle("관심종목", "종목코드만 저장합니다. 현재 시세는 조회하지 않습니다.")
        if (data.watchlist.isEmpty()) EmptyCard("관심종목을 추가해 보세요.")
        data.watchlist.forEach { symbol ->
            WatchlistRow(symbol, data.mode == AppMode.DEMO) { onRemoveWatchCode(symbol) }
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(15.dp), verticalArrangement = Arrangement.spacedBy(9.dp)) {
                Text("관심종목 추가", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                OutlinedTextField(watchCode, { onWatchCodeChange(it.filter(Char::isDigit).take(6)) }, label = { Text("종목코드 6자리") }, singleLine = true, keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number), modifier = Modifier.fillMaxWidth())
                OutlinedButton(onClick = onAddWatchCode, enabled = !busy, modifier = Modifier.fillMaxWidth().height(46.dp)) { Text("관심종목 저장") }
            }
        }
    }
}

@Composable
private fun HoldingCard(holding: Holding, demo: Boolean, onDelete: () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Row(Modifier.fillMaxWidth().padding(start = 14.dp, top = 12.dp, bottom = 12.dp, end = 7.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("${holding.stockCode} · ${stockName(holding.stockCode)}", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                Text("${holding.quantity}주 · 평균매입가 ${formatKrw(holding.averagePrice)}", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
                Text(if (demo) "예시/데모 저장" else "현재가 · 미조회", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 11.sp)
            }
            TextButton(onClick = onDelete) {
                Icon(Icons.Outlined.DeleteOutline, contentDescription = null, modifier = Modifier.size(18.dp))
                Spacer(Modifier.width(3.dp))
                Text("삭제")
            }
        }
    }
}

@Composable
private fun WatchlistRow(code: String, demo: Boolean, onDelete: () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Row(Modifier.fillMaxWidth().padding(start = 14.dp, top = 10.dp, bottom = 10.dp, end = 7.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Outlined.StarBorder, contentDescription = null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.size(21.dp))
            Spacer(Modifier.width(9.dp))
            Column(Modifier.weight(1f)) {
                Text("$code · ${stockName(code)}", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                Text(if (demo) "데모 관심종목" else "시세 미조회", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
            }
            TextButton(onClick = onDelete) { Text("삭제") }
        }
    }
}

@Composable
private fun AlertsScreen(reminderOn: Boolean, busy: Boolean, onToggle: (Boolean) -> Unit) {
    ScreenColumn {
        Column {
            Text("가이드 알림", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text("실시간 신호가 아닌 기기 확인 리마인더입니다.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Row(verticalAlignment = Alignment.Top, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    Surface(shape = RoundedCornerShape(12.dp), color = MaterialTheme.colorScheme.primaryContainer) {
                        Icon(Icons.Outlined.NotificationsNone, contentDescription = null, tint = MaterialTheme.colorScheme.primary, modifier = Modifier.padding(9.dp).size(21.dp))
                    }
                    Column(Modifier.weight(1f)) {
                        Text("평일 오전 9:10", fontSize = 16.sp, fontWeight = FontWeight.Bold)
                        Text("공휴일 포함 · 한국 시간 기준", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp, lineHeight = 18.sp)
                    }
                }
                Text("월요일–금요일 · 한국 표준시", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
                Button(onClick = { onToggle(!reminderOn) }, enabled = !busy, modifier = Modifier.fillMaxWidth().height(48.dp)) {
                    Text(if (reminderOn) "리마인더 끄기" else "리마인더 켜기")
                }
                Text(if (reminderOn) "기기 리마인더 켜짐" else "현재 꺼짐 · Android 알림 권한이 필요할 수 있습니다.", fontSize = 12.sp, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        InfoCard("Android가 알림 시각을 늦출 수 있습니다. 시장 움직임이나 매수·매도 신호를 보내지 않습니다.")
    }
}

@Composable
private fun SettingsScreen(
    mode: AppMode,
    serverOk: Boolean,
    marketLabel: String,
    capitalInput: String,
    serverUrl: String,
    apiToken: String,
    savedTokenAvailable: Boolean,
    busy: Boolean,
    onCapitalChange: (String) -> Unit,
    onSaveCapital: () -> Unit,
    onUrlChange: (String) -> Unit,
    onTokenChange: (String) -> Unit,
    onConnect: () -> Unit,
    onRefresh: () -> Unit,
    onSwitchToDemo: () -> Unit,
    onSwitchToAlerts: () -> Unit,
) {
    ScreenColumn {
        Column {
            Text("설정", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.Bold)
            Text("저장할 위치와 외부 연결을 확인해요.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("투자 기준금액", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Text("가이드 계산의 기준으로 입력합니다. 계좌 잔고나 평가금액을 조회한 값이 아닙니다.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, lineHeight = 17.sp)
                OutlinedTextField(
                    value = capitalInput,
                    onValueChange = { onCapitalChange(it.filter { c -> c.isDigit() || c == ',' }.take(18)) },
                    label = { Text("원 단위") },
                    placeholder = { Text("예: 10000000") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    modifier = Modifier.fillMaxWidth().testTag("account-equity"),
                )
                Button(onClick = onSaveCapital, enabled = !busy, modifier = Modifier.fillMaxWidth().height(46.dp)) { Text("기준금액 저장") }
            }
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("저장 모드", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Text(if (mode == AppMode.DEMO) "오프라인 데모 · 이 기기에만 저장" else "서버 모드 · 서버 응답을 사용", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp)
                if (mode == AppMode.DEMO) {
                    Text("실제 서버에 연결하면 보유·관심 정보는 서버 API에 저장됩니다. 오류가 나면 데모 자료로 바꾸지 않습니다.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, lineHeight = 17.sp)
                } else {
                    Text(if (serverOk) "서버 상태 확인됨 · $marketLabel" else "서버 연결 상태 확인 필요 · 데모 자료를 사용하지 않습니다.", color = if (serverOk) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.tertiary, fontSize = 12.sp, lineHeight = 17.sp)
                    OutlinedButton(onClick = onRefresh, enabled = !busy, modifier = Modifier.fillMaxWidth()) { Text("서버 상태 다시 확인") }
                    OutlinedButton(onClick = onSwitchToDemo, enabled = !busy, modifier = Modifier.fillMaxWidth()) { Text("오프라인 데모 모드로 전환") }
                }
            }
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("개인 서버 연결", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Text("서버 주소와 개인 API 키로 연결합니다. 개인 API 키는 암호화해 이 기기에 저장합니다.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, lineHeight = 17.sp)
                OutlinedTextField(
                    value = serverUrl,
                    onValueChange = onUrlChange,
                    label = { Text("서버 기본 주소") },
                    placeholder = { Text("https://your-server.example") },
                    singleLine = true,
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
                    modifier = Modifier.fillMaxWidth(),
                )
                OutlinedTextField(
                    value = apiToken,
                    onValueChange = onTokenChange,
                    label = { Text("개인 API 키") },
                    placeholder = { Text(if (savedTokenAvailable) "저장된 키를 유지하려면 비워 두세요" else "서버에서 만든 32자 이상 키") },
                    singleLine = true,
                    visualTransformation = PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    modifier = Modifier.fillMaxWidth(),
                )
                Button(onClick = onConnect, enabled = !busy, modifier = Modifier.fillMaxWidth().height(48.dp)) {
                    if (busy && mode == AppMode.SERVER) CircularProgressIndicator(Modifier.size(18.dp), strokeWidth = 2.dp, color = MaterialTheme.colorScheme.onPrimary)
                    else Text(if (mode == AppMode.SERVER) "설정 저장 후 연결 확인" else "서버 모드로 연결")
                }
                Text(
                    if (BuildConfig.DEBUG) "디버그 빌드: localhost·에뮬레이터·사설망 HTTP 또는 HTTPS" else "릴리스 빌드: HTTPS 서버만 허용",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 12.sp,
                )
            }
        }
        Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("기기 리마인더", fontWeight = FontWeight.Bold, fontSize = 15.sp)
                Text("평일 오전 9시 10분 알림을 알림 탭에서 켜고 끌 수 있습니다. 실시간 투자 신호는 아닙니다.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, lineHeight = 17.sp)
                OutlinedButton(onClick = onSwitchToAlerts, modifier = Modifier.fillMaxWidth()) { Text("알림 설정 보기") }
            }
        }
        Text("토스증권 인증정보는 앱에 입력하지 않습니다. 실제 매매는 증권사 앱에서 직접 확인하세요.", color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, lineHeight = 17.sp)
    }
}

@Composable
private fun SectionTitle(title: String, subtitle: String) {
    Column(verticalArrangement = Arrangement.spacedBy(3.dp)) {
        Text(title, fontWeight = FontWeight.Bold, fontSize = 17.sp)
        Text(subtitle, color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, lineHeight = 17.sp)
    }
}

@Composable
private fun EmptyCard(message: String) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Text(message, modifier = Modifier.fillMaxWidth().padding(16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 13.sp)
    }
}

@Composable
private fun InfoCard(message: String) {
    Surface(shape = RoundedCornerShape(13.dp), color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.75f)) {
        Text(message, modifier = Modifier.fillMaxWidth().padding(13.dp), color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 12.sp, lineHeight = 17.sp)
    }
}

@Composable
private fun StatusPill(text: String, color: Color) {
    Surface(shape = CircleShape, color = color) {
        Text(text, modifier = Modifier.padding(horizontal = 9.dp, vertical = 5.dp), color = MaterialTheme.colorScheme.onSurface, fontSize = 11.sp, fontWeight = FontWeight.SemiBold)
    }
}

private fun stockName(code: String): String = when (code) {
    "005930" -> "삼성전자"
    "000660" -> "SK하이닉스"
    "035420" -> "NAVER"
    "005380" -> "현대차"
    else -> code
}

private fun plainNumber(amount: Double): String = NumberFormat.getIntegerInstance(Locale.KOREA).format(amount.toLong())
