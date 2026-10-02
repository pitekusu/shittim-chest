package dev.pitekusu.shittim.records

import android.annotation.SuppressLint
import android.content.Context
import android.content.ContextWrapper
import androidx.activity.compose.LocalActivityResultRegistryOwner
import androidx.activity.compose.setContent
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.Configuration
import androidx.work.CoroutineWorker
import androidx.work.Data
import androidx.work.ListenableWorker
import androidx.work.NetworkType
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.impl.WorkManagerImpl
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.auth.StoredToken
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import io.ktor.client.engine.mock.MockEngine
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.time.Instant
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class BootstrapRefreshTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @SuppressLint("RestrictedApi") // Restore the WorkManager test delegate; never close the Activity's real manager.
  @Test fun manualRefreshQueuesOfflineDeltaSyncWithoutResettingTheSavedListAndRejectsStaleCallbacks() {
    val app = compose.activity.applicationContext
    val directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "refresh-").toFile()
    val context = object : ContextWrapper(compose.activity) {
      override fun getApplicationContext(): Context = this
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = "${app.packageName}.${directory.name}"
      override fun getDatabasePath(name: String): File = File(directory, name)
    }
    val originalManager = WorkManager.getInstance(app) as WorkManagerImpl
    val owner = ViewModelStore()
    val accountId = "u".repeat(43)
    val recordId = "r".repeat(43)
    val now = Instant.ofEpochSecond(Instant.now().epochSecond)
    val deadline = now.plusSeconds(600)
    var stored: StoredToken? = StoredToken("t".repeat(43), deadline, CacheAuthorization(accountId, now, deadline))
    val client = MobileAuthClient(MockEngine { throw IOException("fake_offline") })
    val workerGate = CompletableDeferred<Unit>()
    val starts = AtomicInteger()
    val factory = object : WorkerFactory() {
      override fun createWorker(appContext: Context, workerClassName: String,
        workerParameters: WorkerParameters): ListenableWorker {
        check(workerClassName == RecordSyncWorker::class.java.name)
        return object : CoroutineWorker(appContext, workerParameters) {
          override suspend fun doWork(): Result {
            starts.incrementAndGet()
            workerGate.await()
            return Result.success(Data.Builder().putLong("finishedAt", now.toEpochMilli()).build())
          }
        }
      }
    }
    var testManager: WorkManager? = null
    try {
      WorkManagerTestInitHelper.initializeTestWorkManager(context, Configuration.Builder()
        .setExecutor(SynchronousExecutor()).setWorkerFactory(factory).build())
      val manager = WorkManager.getInstance(context)
      testManager = manager
      val driver = checkNotNull(WorkManagerTestInitHelper.getTestDriver(context))
      fun requests() = manager.getWorkInfosForUniqueWork("records-immediate-sync-v1").get(10, TimeUnit.SECONDS)
      fun unfinished() = requests().filterNot { it.state.isFinished }
      runBlocking {
        RecordCacheAccount.activate(context, accountId)
        val database = EncryptedRecordsDatabase.open(context)
        try {
          val cache = EncryptedRecordStore(database, RecordDataKeyProtector(KeystorePrivateKeyStore(context)))
          val entry = RecordListEntry(recordId, "架空の保存済み記録", "架空の依頼者",
            RecordAvatar(null, "cyan"), now, "アロナ")
          val payload = """{"schemaVersion":1,"entry":${Json.encodeToString(entry)}}""".toByteArray()
          try { cache.save(accountId, recordId, CachedRecordPart.LIST, payload) }
          finally { payload.fill(0) }
        } finally { database.close() }
      }
      lateinit var model: MobileSessionModel
      var rendered: BootstrapScreen.State? = null
      compose.activityRule.scenario.onActivity { activity ->
        model = MobileSessionModel(client, { stored }, { stored = it }, { stored = null },
          { expected -> stored?.takeIf { it.accessToken == expected }?.let {
            stored = StoredToken(it.accessToken, it.expiresAt)
          } }, {}, { false }, {}, { RecordCacheAccount.activate(context, it) }, { RecordCacheAccount.clear(context) })
        owner.put("session", model)
        val presenter = BootstrapPresenter(model)
        activity.setContent {
          CompositionLocalProvider(LocalContext provides context, LocalActivityResultRegistryOwner provides activity) {
            rendered = presenter.present()
          }
        }
      }
      compose.waitUntil(10_000) {
        rendered?.let { it.session == SessionState.Unavailable && it.records is RecordListState.Ready } == true
      }
      compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.SearchRecords("架空")) }
      compose.waitUntil(10_000) {
        rendered?.let { !it.searching && it.listQuery.text == "架空" &&
          (it.records as? RecordListState.Ready)?.loadedIds == setOf(recordId) } == true
      }
      val savedList = rendered!!.records
      val query = rendered!!.listQuery
      val retainedSink = rendered!!.eventSink
      assertTrue(unfinished().isEmpty())
      // A retained list callback cannot refresh after navigation, even before recomposition.
      compose.runOnIdle {
        model.openDestination("/records/$recordId")
        retainedSink(BootstrapScreen.Event.RefreshRecords)
      }
      assertTrue(unfinished().isEmpty())
      compose.waitUntil(10_000) { rendered?.selectedRecordId == recordId }
      compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.RefreshRecords) }
      assertTrue(unfinished().isEmpty())
      compose.runOnIdle { model.closeDestination() }
      compose.waitUntil(10_000) { rendered?.selectedRecordId == null }
      compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.RefreshRecords) }
      val queued = unfinished().single()
      assertEquals(WorkInfo.State.ENQUEUED, queued.state)
      assertEquals(NetworkType.CONNECTED, queued.constraints.requiredNetworkType)
      assertEquals(0, starts.get())
      compose.runOnIdle {
        assertEquals(RecordSyncState.Idle, rendered!!.sync)
        assertSame(savedList, rendered!!.records)
        assertEquals(query, rendered!!.listQuery)
        rendered!!.eventSink(BootstrapScreen.Event.RefreshRecords)
      }
      assertEquals(queued.id, unfinished().single().id) // KEEP coalesces repeated offline pulls.
      driver.setAllConstraintsMet(queued.id)
      compose.waitUntil(10_000) { rendered?.sync == RecordSyncState.Running && starts.get() == 1 }
      compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.RefreshRecords) }
      assertEquals(queued.id, unfinished().single().id)
      assertEquals(1, starts.get())
      workerGate.complete(Unit)
      compose.waitUntil(10_000) { rendered?.sync == RecordSyncState.Completed }
      compose.runOnIdle {
        assertSame(savedList, rendered!!.records)
        assertEquals(query, rendered!!.listQuery)
        model.logout()
        retainedSink(BootstrapScreen.Event.RefreshRecords) // Live permit is gone before the next frame.
      }
      assertTrue(unfinished().isEmpty())
      compose.waitUntil(10_000) {
        rendered?.let { it.session is SessionState.SignedOut && !it.canReadRecords } == true
      }
      compose.runOnIdle {
        assertEquals(RecordListQuery(), rendered!!.listQuery)
      }
      compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.RefreshRecords) }
      assertTrue(unfinished().isEmpty())
    } finally {
      compose.activityRule.scenario.onActivity { it.setContent {}; owner.clear() }
      workerGate.complete(Unit)
      try {
        testManager?.cancelAllWork()?.result?.get(10, TimeUnit.SECONDS)
        if (testManager != null) WorkManagerTestInitHelper.closeWorkDatabase()
      } finally {
        WorkManagerImpl.setDelegate(originalManager)
        client.close()
        runBlocking { RecordCacheAccount.clear(context) }
        directory.deleteRecursively()
      }
    }
  }
}
