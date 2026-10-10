package dev.pitekusu.shittim.records

import androidx.test.ext.junit.runners.AndroidJUnit4
import java.util.Locale
import java.util.TimeZone
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DebateRequestDateTimeTest {
  @Test fun utcWithFractionalSecondsIsShownInJapanToTheMinute() {
    assertEquals("2026年10月6日 22:35", formatDebateRequestDateTime("2026-10-06T13:35:45.952374Z"))
  }

  @Test fun japanMidnightAndYearBoundaryUseTheConvertedDate() {
    assertEquals("2026年10月6日 23:59", formatDebateRequestDateTime("2026-10-06T14:59:59Z"))
    assertEquals("2026年10月7日 00:00", formatDebateRequestDateTime("2026-10-06T15:00:00Z"))
    assertEquals("2027年1月1日 00:00", formatDebateRequestDateTime("2026-12-31T15:00:00Z"))
  }

  @Test fun deviceTimeZoneAndLocaleDoNotChangeJapanTime() {
    val previousZone = TimeZone.getDefault()
    val previousLocale = Locale.getDefault()
    try {
      TimeZone.setDefault(TimeZone.getTimeZone("America/Los_Angeles"))
      Locale.setDefault(Locale.US)
      assertEquals("2026年10月6日 22:35", formatDebateRequestDateTime("2026-10-06T13:35:45Z"))
    } finally {
      TimeZone.setDefault(previousZone)
      Locale.setDefault(previousLocale)
    }
  }
}
