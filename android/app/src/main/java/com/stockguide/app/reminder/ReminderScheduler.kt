package com.stockguide.app.reminder

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import java.time.ZonedDateTime

class ReminderScheduler(private val context: Context) {
    private val alarmManager = context.getSystemService(AlarmManager::class.java)

    fun scheduleNext(now: ZonedDateTime = ZonedDateTime.now(ReminderSchedule.zone)) {
        val triggerAt = ReminderSchedule.nextWeekday(now).toInstant().toEpochMilli()
        alarmManager.set(AlarmManager.RTC_WAKEUP, triggerAt, reminderPendingIntent())
    }

    fun cancel() {
        alarmManager.cancel(reminderPendingIntent())
    }

    private fun reminderPendingIntent(): PendingIntent {
        val intent = Intent(context, ReminderReceiver::class.java)
        return PendingIntent.getBroadcast(
            context,
            REQUEST_CODE,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
    }

    private companion object { const val REQUEST_CODE = 910 }
}
