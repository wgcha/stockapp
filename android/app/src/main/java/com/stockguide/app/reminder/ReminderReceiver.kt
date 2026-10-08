package com.stockguide.app.reminder

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.stockguide.app.MainActivity

class ReminderReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val prefs = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)
        if (!prefs.getBoolean(KEY_ENABLED, false)) return

        ensureChannel(context)
        val hasPermission = Build.VERSION.SDK_INT < 33 ||
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
        if (hasPermission) {
            val openApp = android.app.PendingIntent.getActivity(
                context,
                911,
                Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
                android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE,
            )
            val notification = NotificationCompat.Builder(context, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.ic_menu_info_details)
                .setContentTitle("오늘의 가이드를 확인해 보세요")
                .setContentText("이 알림은 기기에서 설정한 확인 리마인더입니다. 실시간 투자 신호가 아닙니다.")
                .setStyle(NotificationCompat.BigTextStyle().bigText("주식 가이드 앱에서 자료 연결과 오늘의 안내를 확인해 보세요. 실시간 매매 신호나 주문 알림이 아닙니다."))
                .setContentIntent(openApp)
                .setAutoCancel(true)
                .build()
            context.getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification)
        }
        ReminderScheduler(context).scheduleNext()
    }

    private fun ensureChannel(context: Context) {
        val channel = NotificationChannel(CHANNEL_ID, "가이드 확인 리마인더", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "평일에 앱에서 가이드를 확인하도록 알려주는 기기 알림"
        }
        context.getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    companion object {
        const val PREFERENCES = "local_reminder_v1"
        const val KEY_ENABLED = "enabled"
        const val CHANNEL_ID = "guide_reminder"
        const val NOTIFICATION_ID = 910
    }
}

class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        if (intent?.action !in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_TIME_CHANGED, Intent.ACTION_TIMEZONE_CHANGED)) return
        val enabled = context.getSharedPreferences(ReminderReceiver.PREFERENCES, Context.MODE_PRIVATE)
            .getBoolean(ReminderReceiver.KEY_ENABLED, false)
        if (enabled) ReminderScheduler(context).scheduleNext()
    }
}
