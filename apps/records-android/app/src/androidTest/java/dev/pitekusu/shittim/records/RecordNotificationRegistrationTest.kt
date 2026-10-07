package dev.pitekusu.shittim.records

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.work.Configuration
import androidx.work.CoroutineWorker
import androidx.work.ListenableWorker
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.impl.WorkManagerImpl
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordNotificationRegistrationTest {
  @Test fun callbackKeepsRunningRegistrationAndForegroundRefreshWaitsItsCompletion() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    val originalManager = WorkManager.getInstance(context) as WorkManagerImpl
    // Only the configured-state check needs a Firebase app. No SDK registration/network is run.
    val firebase = FirebaseApp.initializeApp(context, FirebaseOptions.Builder()
      .setApplicationId("1:000000000000:android:0123456789abcdef012345")
      .setProjectId("synthetic-record-callback").setApiKey("synthetic-not-a-real-key").build(),
      "record-registration-callback-test")
    val started = CompletableDeferred<Unit>()
    val gate = CompletableDeferred<Unit>()
    val starts = AtomicInteger()
    var running: ListenableWorker? = null
    val factory = object : WorkerFactory() {
      override fun createWorker(appContext: Context, workerClassName: String,
        workerParameters: WorkerParameters): ListenableWorker {
        check(workerClassName == RecordNotificationWorker::class.java.name)
        return object : CoroutineWorker(appContext, workerParameters) {
          override suspend fun doWork(): Result {
            starts.incrementAndGet()
            started.complete(Unit)
            gate.await()
            return Result.success()
          }
        }.also { running = it }
      }
    }
    var manager: WorkManager? = null
    val settings = RecordNotificationSettings(context)
    try {
      settings.optIn()
      settings.bindingFor("t".repeat(43), "old-fid")
      WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
        .setExecutor(SynchronousExecutor()).setWorkerFactory(factory).build())
      val workManager = WorkManager.getInstance(context)
      manager = workManager
      fun requests() = workManager.getWorkInfosForUniqueWork("records-notification-registration-v1")
        .get(10, TimeUnit.SECONDS)
      RecordNotificationRegistration.schedule(context)
      val request = requests().single { !it.state.isFinished }
      checkNotNull(WorkManagerTestInitHelper.getTestDriver(context)).setAllConstraintsMet(request.id)
      runBlocking { withTimeout(10_000) { started.await() } }
      assertEquals(WorkInfo.State.RUNNING, workManager.getWorkInfoById(request.id).get()?.state)

      repeat(2) { RecordNotificationRegistration.onRegistered(context, "new-fid") }
      assertEquals(listOf(request.id), requests().filterNot { it.state.isFinished }.map { it.id })
      assertEquals(WorkInfo.State.RUNNING, workManager.getWorkInfoById(request.id).get()?.state)
      assertFalse(checkNotNull(running).isStopped)
      assertEquals(1, starts.get())

      // A permission change on foreground must run after, not cancel or race, a pending DELETE.
      RecordNotificationRegistration.schedule(context, afterCurrent = true)
      val followUp = requests().single { it.id != request.id }
      assertFalse(checkNotNull(running).isStopped)
      assertEquals(1, starts.get())
      gate.complete(Unit)
      val completed = runBlocking { withTimeout(10_000) {
        workManager.getWorkInfoByIdFlow(request.id).first { it?.state?.isFinished == true }
      } }
      assertEquals(WorkInfo.State.SUCCEEDED, completed?.state)
      checkNotNull(WorkManagerTestInitHelper.getTestDriver(context)).setAllConstraintsMet(followUp.id)
      val refreshed = runBlocking { withTimeout(10_000) {
        workManager.getWorkInfoByIdFlow(followUp.id).first { it?.state?.isFinished == true }
      } }
      assertEquals(WorkInfo.State.SUCCEEDED, refreshed?.state)
      assertEquals(2, starts.get())
    } finally {
      gate.complete(Unit)
      try {
        manager?.cancelAllWork()?.result?.get(10, TimeUnit.SECONDS)
        if (manager != null) WorkManagerTestInitHelper.closeWorkDatabase()
      } finally {
        WorkManagerImpl.setDelegate(originalManager)
        settings.revoke()
        firebase.delete()
      }
    }
  }
}
