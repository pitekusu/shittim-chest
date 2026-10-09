package dev.pitekusu.shittim.records

import android.content.Context
import android.Manifest
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
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.StoredToken
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.headersOf
import java.time.Instant
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordNotificationRegistrationTest {
  private fun authorize(context: Context) {
    val now = Instant.now().truncatedTo(java.time.temporal.ChronoUnit.SECONDS)
    InstrumentationRegistry.getInstrumentation().uiAutomation.grantRuntimePermission(
      context.packageName, Manifest.permission.POST_NOTIFICATIONS)
    val store = KeystoreTokenStore(context)
    store.clear()
    store.completeLogout()
    store.save(StoredToken("t".repeat(43), now.plusSeconds(3600),
      CacheAuthorization("a".repeat(43), now.minusSeconds(60), now.plusSeconds(3600))))
    RecordNotifications.locallyRevoked = false
  }

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
      authorize(context)
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
        KeystoreTokenStore(context).clear()
        firebase.delete()
      }
    }
  }

  @Test fun lateSuccessBeforeFailingWorkReturnsWaitsForTerminalAndDoesNotLoop() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    val original = WorkManager.getInstance(context) as WorkManagerImpl
    val firebase = FirebaseApp.initializeApp(context, FirebaseOptions.Builder()
      .setApplicationId("1:000000000000:android:0123456789abcdef012345")
      .setProjectId("synthetic-record-late").setApiKey("synthetic-not-a-real-key").build(), "record-late-test")
    val starts = AtomicInteger()
    val started = CompletableDeferred<Unit>()
    val recoveryStarted = CompletableDeferred<Unit>()
    val fail = CompletableDeferred<Unit>()
    val factory = object : WorkerFactory() {
      override fun createWorker(appContext: Context, workerClassName: String, parameters: WorkerParameters): ListenableWorker {
        if (workerClassName == RecordNotificationRecoveryWorker::class.java.name) {
          return object : CoroutineWorker(appContext, parameters) {
            override suspend fun doWork(): Result {
              // Observe entry directly instead of relying on a Flow's RUNNING emission.
              recoveryStarted.complete(Unit)
              return RecordNotificationRecoveryWorker(appContext, parameters).doWork()
            }
          }
        }
        check(workerClassName == RecordNotificationWorker::class.java.name)
        return object : CoroutineWorker(appContext, parameters) {
          override suspend fun doWork(): Result {
            if (starts.getAndIncrement() == 0) {
              started.complete(Unit)
              fail.await()
              return Result.failure()
            }
            return registerRecordNotifications(context, 0,
              client = { MobileAuthClient(MockEngine {
                respond("""{"schemaVersion":1,"expiresAt":"${Instant.now().plusSeconds(300)}"}""",
                  headers = headersOf("Content-Type", "application/json"))
              }) }, installation = {
                RecordNotificationRegistration.onRegistered(context, "late-fid")
                "late-fid"
              })
          }
        }
      }
    }
    try {
      authorize(context)
      val settings = RecordNotificationSettings(context)
      settings.optIn()
      settings.failure(NotificationRegistrationStage.FIREBASE, NotificationRegistrationFailure.NETWORK, 4, terminal = true)
      WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
        .setExecutor(SynchronousExecutor()).setWorkerFactory(factory).build())
      val manager = WorkManager.getInstance(context)
      val driver = checkNotNull(WorkManagerTestInitHelper.getTestDriver(context))
      fun requests() = manager.getWorkInfosForUniqueWork("records-notification-registration-v1").get(10, TimeUnit.SECONDS)
      fun states(name: String): String = try {
        manager.getWorkInfosForUniqueWork(name).get(1, TimeUnit.SECONDS).map { it.state }.toString()
      } catch (error: Exception) { error.javaClass.simpleName }
      fun <T> await(stage: String, block: suspend () -> T): T = runBlocking {
        try { withTimeout(10_000) { block() } }
        catch (error: TimeoutCancellationException) {
          throw AssertionError("Timed out at $stage; " +
            "records-notification-registration-v1=${states("records-notification-registration-v1")}; " +
            "${RecordNotificationRegistration.RECOVERY_WORK}=${states(RecordNotificationRegistration.RECOVERY_WORK)}; " +
            "registrationStarts=${starts.get()}", error)
        }
      }
      RecordNotificationRegistration.schedule(context)
      val failed = requests().single()
      driver.setAllConstraintsMet(failed.id)
      await("initial_registration_entered") { started.await() }
      assertEquals(WorkInfo.State.RUNNING, manager.getWorkInfoById(failed.id).get()?.state)
      assertEquals(null, settings.binding)
      RecordNotificationRegistration.onRegistered(context, "late-fid")
      val waiting = manager.getWorkInfosForUniqueWork(RecordNotificationRegistration.RECOVERY_WORK).get(10, TimeUnit.SECONDS).single()
      driver.setAllConstraintsMet(waiting.id)
      await("recovery_worker_entered") { recoveryStarted.await() }
      assertEquals(WorkInfo.State.RUNNING, manager.getWorkInfoById(waiting.id).get()?.state)
      assertEquals(1, requests().size) // No dependent that would inherit the imminent failure.
      fail.complete(Unit)
      await("initial_registration_failed") {
        manager.getWorkInfoByIdFlow(failed.id).first { it?.state == WorkInfo.State.FAILED }
      }
      val recovery = await("follow_up_enqueued") {
        manager.getWorkInfosForUniqueWorkFlow("records-notification-registration-v1")
          .first { work -> work.any { it.id != failed.id && it.state == WorkInfo.State.ENQUEUED } }
          .single { it.id != failed.id && !it.state.isFinished }
      }
      assertEquals(WorkInfo.State.ENQUEUED, recovery.state)
      driver.setAllConstraintsMet(recovery.id)
      await("follow_up_succeeded") {
        manager.getWorkInfoByIdFlow(recovery.id).first { it?.state == WorkInfo.State.SUCCEEDED }
      }
      assertFalse(settings.failed)
      checkNotNull(settings.binding)
      await("recovery_work_finished") {
        manager.getWorkInfosForUniqueWorkFlow(RecordNotificationRegistration.RECOVERY_WORK).first { work -> work.all { it.state.isFinished } }
      }
      val count = requests().size
      RecordNotificationRegistration.onRegistered(context, "late-fid")
      assertEquals(count, requests().size)
    } finally {
      fail.complete(Unit)
      WorkManager.getInstance(context).cancelAllWork().result.get(10, TimeUnit.SECONDS)
      WorkManagerTestInitHelper.closeWorkDatabase()
      WorkManagerImpl.setDelegate(original)
      RecordNotificationSettings(context).revoke()
      KeystoreTokenStore(context).clear()
      firebase.delete()
    }
  }
}
