package dev.pitekusu.shittim.records

import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordAppLinkTest {
  @Test fun acceptsOnlyCanonicalRecordLinks() {
    val id = "a".repeat(43)
    val path = "/records/$id"
    assertEquals(path, recordDestination("https://shittim.pitekusu.dev$path"))
    for (url in listOf(
      null,
      "https://shittim.pitekusu.dev/records/short",
      "https://shittim.pitekusu.dev$path?next=/admin",
      "https://shittim.pitekusu.dev$path#fragment",
      "https://shittim.pitekusu.dev/records/%61${"a".repeat(42)}",
      "https://shittim.pitekusu.dev.evil.example$path",
      "http://shittim.pitekusu.dev$path",
      "https://shittim.pitekusu.dev:443$path",
    )) assertNull(recordDestination(url))
  }
}
