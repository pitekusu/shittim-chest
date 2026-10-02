package dev.pitekusu.shittim.records

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.Data
import androidx.work.WorkInfo
import java.util.UUID
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordSyncSchedulerTest {
  @Test fun waitingForConnectivityOrRetryDoesNotReportAnActiveRefresh() {
    val completed = work(WorkInfo.State.SUCCEEDED, Data.Builder().putLong("finishedAt", 1).build())
    for (waiting in listOf(WorkInfo.State.ENQUEUED, WorkInfo.State.BLOCKED)) {
      assertEquals(RecordSyncState.Idle,
        RecordSyncScheduler.state(listOf(completed), listOf(work(waiting))))
    }
    assertEquals(RecordSyncState.Idle,
      RecordSyncScheduler.state(listOf(work(WorkInfo.State.ENQUEUED)), emptyList()))
  }

  @Test fun eitherRunningWorkerReportsRefreshEvenWhenAnotherRequestIsQueued() {
    val running = work(WorkInfo.State.RUNNING)
    val queued = work(WorkInfo.State.ENQUEUED)
    assertEquals(RecordSyncState.Running, RecordSyncScheduler.state(listOf(running), listOf(queued)))
    assertEquals(RecordSyncState.Running, RecordSyncScheduler.state(listOf(queued), listOf(running)))
  }

  @Test fun terminalResultsClearRefreshAndPreserveTheLatestFailure() {
    val completed = work(WorkInfo.State.SUCCEEDED, Data.Builder().putLong("finishedAt", 1).build())
    val failed = work(WorkInfo.State.FAILED, Data.Builder().putLong("finishedAt", 2)
      .putString("failure", RecordReadFailure.UNAVAILABLE.name).build())
    assertEquals(RecordSyncState.Completed, RecordSyncScheduler.state(emptyList(), listOf(completed)))
    assertEquals(RecordReadFailure.UNAVAILABLE,
      (RecordSyncScheduler.state(listOf(completed), listOf(failed)) as RecordSyncState.Failed).reason)
    assertEquals(RecordSyncState.Idle,
      RecordSyncScheduler.state(emptyList(), listOf(work(WorkInfo.State.CANCELLED))))
  }

  private fun work(state: WorkInfo.State, output: Data = Data.EMPTY) =
    WorkInfo(UUID.randomUUID(), state, emptySet(), output)
}
