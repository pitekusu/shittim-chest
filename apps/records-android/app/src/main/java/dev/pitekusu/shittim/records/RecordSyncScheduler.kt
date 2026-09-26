package dev.pitekusu.shittim.records

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.Flow

internal object RecordSyncScheduler {
  private const val PERIODIC = "records-periodic-sync-v1"
  private const val IMMEDIATE = "records-immediate-sync-v1"
  private val network = Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()

  fun schedule(context: Context) {
    val manager = WorkManager.getInstance(context)
    manager.enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP,
      PeriodicWorkRequestBuilder<RecordSyncWorker>(15, TimeUnit.MINUTES)
        .setInitialDelay(15, TimeUnit.MINUTES)
        .setConstraints(network).setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
    syncNow(context)
  }

  fun syncNow(context: Context) {
    WorkManager.getInstance(context).enqueueUniqueWork(IMMEDIATE, ExistingWorkPolicy.KEEP,
      OneTimeWorkRequestBuilder<RecordSyncWorker>().setConstraints(network)
        .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS).build())
  }

  fun cancel(context: Context) {
    val manager = WorkManager.getInstance(context)
    manager.cancelUniqueWork(IMMEDIATE)
    manager.cancelUniqueWork(PERIODIC)
  }

  fun states(context: Context): Flow<RecordSyncState> {
    val manager = WorkManager.getInstance(context)
    return combine(manager.getWorkInfosForUniqueWorkFlow(PERIODIC),
      manager.getWorkInfosForUniqueWorkFlow(IMMEDIATE)) { periodic, immediate ->
      val all = periodic + immediate
      when {
        all.any { it.state == WorkInfo.State.RUNNING } -> RecordSyncState.Running
        immediate.any { it.state == WorkInfo.State.ENQUEUED || it.state == WorkInfo.State.BLOCKED } -> RecordSyncState.Idle
        else -> {
          val finished = all.maxByOrNull { it.outputData.getLong("finishedAt", 0) }
          val failure = finished?.outputData?.getString("failure")?.let { name ->
            RecordReadFailure.entries.firstOrNull { it.name == name }
          }
          when {
            failure != null -> RecordSyncState.Failed(failure)
            (finished?.outputData?.getLong("finishedAt", 0) ?: 0) > 0 -> RecordSyncState.Completed
            else -> RecordSyncState.Idle
          }
        }
      }
    }
  }
}
