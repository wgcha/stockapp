package com.stockguide.app.reminder

import java.time.DayOfWeek
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZonedDateTime
import org.junit.Assert.assertEquals
import org.junit.Test

class ReminderScheduleTest {
    private val kst = ZoneId.of("Asia/Seoul")

    @Test fun beforeWeekdayReminderUsesSameDay() {
        val fridayMorning = ZonedDateTime.of(LocalDateTime.of(2026, 10, 9, 9, 0), kst)
        val next = ReminderSchedule.nextWeekday(fridayMorning)
        assertEquals(LocalDateTime.of(2026, 10, 9, 9, 10), next.toLocalDateTime())
    }

    @Test fun afterFridayReminderMovesToMonday() {
        val fridayLate = ZonedDateTime.of(LocalDateTime.of(2026, 10, 9, 9, 11), kst)
        val next = ReminderSchedule.nextWeekday(fridayLate)
        assertEquals(DayOfWeek.MONDAY, next.dayOfWeek)
        assertEquals(LocalDateTime.of(2026, 10, 12, 9, 10), next.toLocalDateTime())
    }

    @Test fun weekendNeverSchedulesOnWeekend() {
        val saturday = ZonedDateTime.of(LocalDateTime.of(2026, 10, 10, 8, 0), kst)
        val next = ReminderSchedule.nextWeekday(saturday)
        assertEquals(DayOfWeek.MONDAY, next.dayOfWeek)
        assertEquals(LocalDateTime.of(2026, 10, 12, 9, 10), next.toLocalDateTime())
    }
}
