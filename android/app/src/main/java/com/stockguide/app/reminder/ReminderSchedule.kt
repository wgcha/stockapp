package com.stockguide.app.reminder

import java.time.DayOfWeek
import java.time.LocalDate
import java.time.LocalTime
import java.time.ZoneId
import java.time.ZonedDateTime

object ReminderSchedule {
    val zone: ZoneId = ZoneId.of("Asia/Seoul")
    private val reminderTime = LocalTime.of(9, 10)

    fun nextWeekday(after: ZonedDateTime): ZonedDateTime {
        val localNow = after.withZoneSameInstant(zone)
        var date: LocalDate = localNow.toLocalDate()
        var candidate = date.atTime(reminderTime).atZone(zone)
        if (!candidate.isAfter(localNow)) date = date.plusDays(1)
        while (date.dayOfWeek == DayOfWeek.SATURDAY || date.dayOfWeek == DayOfWeek.SUNDAY) {
            date = date.plusDays(1)
        }
        return date.atTime(reminderTime).atZone(zone)
    }
}
