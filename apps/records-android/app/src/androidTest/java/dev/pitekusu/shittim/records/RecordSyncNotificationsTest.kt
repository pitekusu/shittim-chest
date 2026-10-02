package dev.pitekusu.shittim.records

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class RecordSyncNotificationsTest {
  @Test fun anUnchangedRunDoesNotInvalidateTheCacheAtStartOrFinish() = runBlocking {
    var notifications = 0
    var timers = 0
    val changes = RecordSyncNotifications(this, { notifications++ }, { 0L }, { timers++ })
    assertFalse(changes.flush())
    assertFalse(changes.close())
    assertEquals(0, notifications)
    assertEquals(0, timers)
  }

  @Test fun trailingSaveAppearsWithinTheWindowEvenWhenTheNextNetworkReadStalls() = runBlocking {
    var time = 0L
    var notifications = 0
    var timers = 0
    val timerStarted = CompletableDeferred<Long>()
    val releaseTimer = CompletableDeferred<Unit>()
    val trailingNotification = CompletableDeferred<Unit>()
    val changes = RecordSyncNotifications(this, {
      notifications++
      if (notifications == 2) trailingNotification.complete(Unit)
    }, { time }, { duration ->
      timers++
      timerStarted.complete(duration)
      releaseTimer.await()
    })
    assertTrue(changes.saved())
    assertEquals(1, notifications)
    time = 100
    assertFalse(changes.saved())
    assertEquals(400L, timerStarted.await())
    time = 499
    assertFalse(changes.saved())
    assertEquals(1, notifications)
    // No subsequent save or sync completion: only the elapsed timer publishes the last change.
    time = 500
    releaseTimer.complete(Unit)
    trailingNotification.await()
    assertEquals(2, notifications)
    assertEquals(1, timers)
    assertFalse(changes.close())
  }

  @Test fun completionFlushesTheBurstAndCancelsItsTimerWithoutDuplicateNotification() = runBlocking {
    var time = 0L
    var notifications = 0
    val timerStarted = CompletableDeferred<Unit>()
    val timerCanceled = CompletableDeferred<Unit>()
    val changes = RecordSyncNotifications(this, { notifications++ }, { time }, {
      try {
        timerStarted.complete(Unit)
        awaitCancellation()
      } finally { timerCanceled.complete(Unit) }
    })
    assertTrue(changes.saved())
    for (value in 1L..499L) {
      time = value
      assertFalse(changes.saved())
    }
    timerStarted.await()
    assertTrue(changes.close())
    timerCanceled.await()
    assertEquals(2, notifications)
    assertFalse(changes.close())
    assertFalse(changes.saved())
  }
}
