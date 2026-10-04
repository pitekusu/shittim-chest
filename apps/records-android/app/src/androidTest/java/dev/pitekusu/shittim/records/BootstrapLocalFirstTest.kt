package dev.pitekusu.shittim.records

import android.annotation.SuppressLint
import android.content.Context
import android.content.ContextWrapper
import android.graphics.Bitmap
import androidx.activity.result.ActivityResultRegistry
import androidx.activity.result.ActivityResultRegistryOwner
import androidx.activity.result.contract.ActivityResultContract
import androidx.activity.compose.setContent
import androidx.activity.compose.LocalActivityResultRegistryOwner
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performScrollToNode
import androidx.core.app.ActivityOptionsCompat
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.Configuration
import androidx.work.WorkManager
import androidx.work.impl.WorkManagerImpl
import androidx.work.testing.SynchronousExecutor
import androidx.work.testing.WorkManagerTestInitHelper
import coil3.SingletonImageLoader
import coil3.asImage
import coil3.memory.MemoryCache
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
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.File
import java.nio.file.Files
import java.time.Instant
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class BootstrapLocalFirstTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun encryptedSavedRecordIsVisibleBeforeNetworkCompletesAndHiddenOnDenial() {
    val app = compose.activity.applicationContext
    val directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "local-first-").toFile()
    // Isolate storage without replacing the Activity's UI context required by Adaptive's window API.
    val context = object : ContextWrapper(compose.activity) {
      override fun getApplicationContext(): Context = this
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = "${app.packageName}.${directory.name}"
      override fun getDatabasePath(name: String): File = File(directory, name)
    }
    val owner = ViewModelStore()
    val accountId = "u".repeat(43)
    val recordId = "r".repeat(43)
    val now = Instant.ofEpochSecond(Instant.now().epochSecond)
    val deadline = now.plusSeconds(600)
    var stored: StoredToken? = StoredToken("t".repeat(43), deadline, CacheAuthorization(accountId, now, deadline))
    val responseGate = CompletableDeferred<Unit>()
    val client = MobileAuthClient(MockEngine {
      responseGate.await()
      respond("{}", HttpStatusCode.Forbidden, headersOf(HttpHeaders.ContentType, "application/json"))
    })
    try {
      runBlocking {
        RecordCacheAccount.activate(context, accountId)
        val database = EncryptedRecordsDatabase.open(context)
        try {
          val cache = EncryptedRecordStore(database, RecordDataKeyProtector(KeystorePrivateKeyStore(context)))
          val entry = RecordListEntry(recordId, "通信前に見える架空の記録", "架空の依頼者",
            RecordAvatar(null, "cyan"), now, "アロナ")
          val payload = """{"schemaVersion":1,"entry":${Json.encodeToString(entry)}}""".toByteArray()
          try { cache.save(accountId, recordId, CachedRecordPart.LIST, payload) }
          finally { payload.fill(0) }
          val preview = RecordPreview(entry.questionPreview, "通信前に見える架空の結論", "アロナ")
          val detail = """{"schemaVersion":1,"preview":${Json.encodeToString(preview)}}""".toByteArray()
          try { cache.save(accountId, recordId, CachedRecordPart.DETAIL, detail) }
          finally { detail.fill(0) }
        } finally { database.close() }
      }
      lateinit var model: MobileSessionModel
      var rendered: BootstrapScreen.State? = null
      compose.activityRule.scenario.onActivity { activity ->
        model = MobileSessionModel(client, { stored }, { stored = it }, { stored = null },
          { expected -> stored?.takeIf { it.accessToken == expected }?.let {
            stored = StoredToken(it.accessToken, it.expiresAt)
          } },
          {}, { false }, {}, { RecordCacheAccount.activate(context, it) }, { RecordCacheAccount.clear(context) })
        owner.put("session", model)
        val presenter = BootstrapPresenter(model)
        activity.setContent {
          CompositionLocalProvider(LocalContext provides context, LocalActivityResultRegistryOwner provides activity) {
            val state = presenter.present()
            rendered = state
            BootstrapUi(state)
          }
        }
      }
      compose.waitUntil(10_000) { rendered?.records is RecordListState.Ready }
      compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText("通信前に見える架空の記録"))
      compose.onNodeWithText("通信前に見える架空の記録").assertIsDisplayed()
      val newRecordId = "n".repeat(43)
      runBlocking {
        val database = EncryptedRecordsDatabase.open(context)
        try {
          val cache = EncryptedRecordStore(database, RecordDataKeyProtector(KeystorePrivateKeyStore(context)))
          val entry = RecordListEntry(newRecordId, "保存直後に見える新しい架空の記録", "架空の依頼者",
            RecordAvatar(null, "cyan"), now.plusSeconds(1), "プラナ")
          val payload = """{"schemaVersion":1,"entry":${Json.encodeToString(entry)}}""".toByteArray()
          try { cache.save(accountId, newRecordId, CachedRecordPart.LIST, payload) }
          finally { payload.fill(0) }
        } finally { database.close() }
      }
      // A committed save, not a WorkManager state transition, wakes the local-first presenter.
      RecordSyncScheduler.cacheChanged()
      compose.waitUntil(10_000) {
        (rendered?.records as? RecordListState.Ready)?.loadedIds?.contains(newRecordId) == true
      }
      compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText("保存直後に見える新しい架空の記録"))
      compose.onNodeWithText("保存直後に見える新しい架空の記録").assertIsDisplayed()
      assertFalse(responseGate.isCompleted)
      compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.SearchRecords("一致しない語句")) }
      compose.waitUntil(10_000) { rendered?.let { !it.searching &&
        (it.records as? RecordListState.Ready)?.loadedIds?.isEmpty() == true } == true }
      compose.onNodeWithTag("bootstrap-content").performScrollToNode(
        hasText(compose.activity.getString(R.string.record_search_empty)))
      compose.onNodeWithText("通信前に見える架空の記録").assertDoesNotExist()
      compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.ClearRecordQuery) }
      compose.waitUntil(10_000) { (rendered?.records as? RecordListState.Ready)?.loadedIds?.contains(recordId) == true }
      compose.onNodeWithTag("records-menu-open").assertIsDisplayed()
      val retainedListSink = rendered!!.eventSink
      compose.runOnIdle { retainedListSink(BootstrapScreen.Event.OpenRecord(recordId)) }
      compose.waitUntil(10_000) { rendered?.let {
        it.selectedRecordId == recordId && it.record is RecordPreviewState.Ready
      } == true }
      val retainedDetailSink = rendered!!.eventSink
      compose.runOnIdle {
        assertEquals(SessionState.Checking, model.state.value)
        assertFalse(responseGate.isCompleted)
        assertTrue(rendered!!.canReadRecords)
        val imageCache = checkNotNull(SingletonImageLoader.get(context).memoryCache)
        imageCache[MemoryCache.Key("restored-avatar-test")] =
          MemoryCache.Value(Bitmap.createBitmap(1, 1, Bitmap.Config.ARGB_8888).asImage())
        responseGate.complete(Unit)
      }
      compose.waitUntil(5_000) { rendered?.let { it.session is SessionState.SignedOut && !it.canReadRecords } == true }
      compose.onNodeWithText("通信前に見える架空の記録").assertDoesNotExist()
      compose.runOnIdle {
        assertNull(SingletonImageLoader.get(context).memoryCache?.get(MemoryCache.Key("restored-avatar-test")))
        assertEquals(RecordListQuery(), rendered!!.listQuery)
        retainedListSink(BootstrapScreen.Event.OpenRecord(recordId))
        retainedDetailSink(BootstrapScreen.Event.CloseRecord)
      }
      compose.runOnIdle { assertNull(rendered!!.selectedRecordId) }
    } finally {
      compose.activityRule.scenario.onActivity { it.setContent {}; owner.clear() }
      client.close()
      runBlocking { RecordCacheAccount.clear(context) }
      directory.deleteRecursively()
    }
  }

  @SuppressLint("RestrictedApi") // Restore the test delegate without closing the Activity's real manager.
  @Test fun accountChangeClearsRestoredRouteAndOfflineNavigationRejectsOldAccountCallbacks() {
    val app = compose.activity.applicationContext
    val directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "nav-owner-").toFile()
    val context = object : ContextWrapper(compose.activity) {
      override fun getApplicationContext(): Context = this
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = "${app.packageName}.${directory.name}"
      override fun getDatabasePath(name: String): File = File(directory, name)
      override fun getSharedPreferences(name: String, mode: Int): android.content.SharedPreferences =
        app.getSharedPreferences("${directory.name}.$name", mode)
    }
    val owner = ViewModelStore()
    val originalManager = WorkManager.getInstance(app) as WorkManagerImpl
    var testManager: WorkManager? = null
    // This route fixture does not test browser launches or OS notification permission dialogs.
    val registryOwner = object : ActivityResultRegistryOwner {
      override val activityResultRegistry = object : ActivityResultRegistry() {
        override fun <I, O> onLaunch(requestCode: Int, contract: ActivityResultContract<I, O>,
          input: I, options: ActivityOptionsCompat?) = Unit
      }
    }
    val accountA = "u".repeat(43)
    val accountB = "v".repeat(43)
    val recordId = "r".repeat(43) // Both accounts may legitimately view the same opaque record.
    val now = Instant.ofEpochSecond(Instant.now().epochSecond)
    val deadline = now.plusSeconds(600)
    var stored: StoredToken? = StoredToken("t".repeat(43), deadline, CacheAuthorization(accountA, now, deadline))
    val responseGate = CompletableDeferred<Unit>()
    var status = HttpStatusCode.OK
    val gets = AtomicInteger()
    val client = MobileAuthClient(MockEngine { request ->
      if (request.url.encodedPath.endsWith("/logout")) respond("", HttpStatusCode.NoContent)
      else {
        gets.incrementAndGet()
        responseGate.await()
        respond("""{"schemaVersion":1,"isAdmin":false,"cacheAccountId":"$accountB","expiresAt":"$deadline",
          "user":{"displayName":"架空の利用者B","avatar":{"kind":"placeholder","alt":"架空","fallbackVariant":"cyan"}}}""",
          status, headersOf(HttpHeaders.ContentType, "application/json"))
      }
    })
    suspend fun saveRecord(accountId: String, label: String) {
      val database = EncryptedRecordsDatabase.open(context)
      try {
        val cache = EncryptedRecordStore(database, RecordDataKeyProtector(KeystorePrivateKeyStore(context)))
        val entry = RecordListEntry(recordId, label, "架空の依頼者", RecordAvatar(null, "cyan"), now, "アロナ")
        val preview = RecordPreview(label, "$label の架空の結論", "アロナ")
        for ((part, payload) in listOf(
          CachedRecordPart.LIST to """{"schemaVersion":1,"entry":${Json.encodeToString(entry)}}""",
          CachedRecordPart.DETAIL to """{"schemaVersion":1,"preview":${Json.encodeToString(preview)}}""")) {
          val bytes = payload.toByteArray()
          try { cache.save(accountId, recordId, part, bytes) }
          finally { bytes.fill(0) }
        }
      } finally { database.close() }
    }
    lateinit var model: MobileSessionModel
    var rendered: BootstrapScreen.State? = null
    try {
      // Network constraints are unmet in the test driver: scheduling cannot run real workers.
      WorkManagerTestInitHelper.initializeTestWorkManager(context,
        Configuration.Builder().setExecutor(SynchronousExecutor()).build())
      testManager = WorkManager.getInstance(context)
      RecordNotificationSettings(context).disable()
      runBlocking {
        RecordCacheAccount.activate(context, accountA)
        saveRecord(accountA, "以前のアカウントの架空の記録")
      }
      compose.activityRule.scenario.onActivity { activity ->
        model = MobileSessionModel(client, { stored }, { stored = it }, { stored = null },
          { expected -> stored?.takeIf { it.accessToken == expected }?.let {
            stored = StoredToken(it.accessToken, it.expiresAt)
          } }, {}, { false }, {}, { verifiedAccount ->
            RecordCacheAccount.activate(context, verifiedAccount)
            saveRecord(verifiedAccount, "新しいアカウントの架空の記録")
          }, { RecordCacheAccount.clear(context) })
        owner.put("session", model)
        model.openDestination("/records/$recordId")
        val presenter = BootstrapPresenter(model)
        activity.setContent {
          CompositionLocalProvider(LocalContext provides context, LocalActivityResultRegistryOwner provides registryOwner) {
            rendered = presenter.present()
          }
        }
      }
      compose.waitUntil(10_000) { rendered?.let {
        it.session == SessionState.Checking && it.canReadRecords && it.selectedRecordId == recordId &&
          it.records is RecordListState.Ready && it.record is RecordPreviewState.Ready
      } == true }
      val oldAccountSink = rendered!!.eventSink
      compose.runOnIdle { responseGate.complete(Unit) }
      compose.waitUntil(10_000) { rendered?.let {
        (it.session as? SessionState.SignedIn)?.cacheAccountId == accountB &&
          (it.records as? RecordListState.Ready)?.loadedIds == setOf(recordId)
      } == true }
      compose.runOnIdle {
        assertNull(rendered!!.selectedRecordId)
        assertEquals(RecordPreviewState.Idle, rendered!!.record)
        oldAccountSink(BootstrapScreen.Event.OpenRecord(recordId))
      }
      compose.runOnIdle { assertNull(rendered!!.selectedRecordId) }
      compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.OpenRecord(recordId)) }
      compose.waitUntil(10_000) { rendered?.let {
        it.selectedRecordId == recordId &&
          (it.record as? RecordPreviewState.Ready)?.preview?.question == "新しいアカウントの架空の記録"
      } == true }
      compose.runOnIdle {
        oldAccountSink(BootstrapScreen.Event.CloseRecord)
        model.onForeground()
      }
      compose.waitUntil(10_000) { gets.get() >= 2 && rendered?.session is SessionState.SignedIn }
      compose.runOnIdle { assertEquals(recordId, rendered!!.selectedRecordId) }
      compose.runOnIdle { status = HttpStatusCode.ServiceUnavailable; model.onForeground() }
      compose.waitUntil(10_000) { rendered?.session == SessionState.Unavailable }
      compose.runOnIdle {
        assertEquals(recordId, rendered!!.selectedRecordId)
        assertTrue(rendered!!.canReadRecords)
        rendered!!.eventSink(BootstrapScreen.Event.CloseRecord)
      }
      compose.waitUntil(10_000) { rendered?.selectedRecordId == null }
      val offlineListSink = rendered!!.eventSink
      compose.runOnIdle { offlineListSink(BootstrapScreen.Event.OpenRecord(recordId)) }
      compose.waitUntil(10_000) { rendered?.selectedRecordId == recordId && rendered?.record is RecordPreviewState.Ready }
      val offlineDetailSink = rendered!!.eventSink
      compose.runOnIdle {
        rendered!!.eventSink(BootstrapScreen.Event.Logout)
        offlineListSink(BootstrapScreen.Event.OpenRecord(recordId))
        offlineDetailSink(BootstrapScreen.Event.CloseRecord)
      }
      compose.waitUntil(10_000) { rendered?.session is SessionState.SignedOut }
      compose.runOnIdle {
        assertNull(rendered!!.selectedRecordId)
        assertFalse(rendered!!.canReadRecords)
        assertEquals(RecordPreviewState.Idle, rendered!!.record)
      }
    } finally {
      compose.activityRule.scenario.onActivity { it.setContent {}; owner.clear() }
      responseGate.complete(Unit)
      client.close()
      try {
        testManager?.cancelAllWork()?.result?.get(10, TimeUnit.SECONDS)
        if (testManager != null) WorkManagerTestInitHelper.closeWorkDatabase()
      } finally {
        WorkManagerImpl.setDelegate(originalManager)
        runBlocking { RecordCacheAccount.clear(context) }
        app.deleteSharedPreferences("${directory.name}.record-notification-v1")
        directory.deleteRecursively()
      }
    }
  }
}
