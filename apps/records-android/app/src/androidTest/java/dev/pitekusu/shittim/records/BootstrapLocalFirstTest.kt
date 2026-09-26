package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import android.graphics.Bitmap
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
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
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
    val context = object : ContextWrapper(app) {
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
        } finally { database.close() }
      }
      lateinit var model: MobileSessionModel
      var rendered: BootstrapScreen.State? = null
      compose.activityRule.scenario.onActivity { activity ->
        model = MobileSessionModel(client, { stored }, { stored = it }, { stored = null },
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
      val logout = compose.activity.getString(R.string.session_local_logout_action)
      compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(logout))
      compose.onNodeWithText(logout).assertIsDisplayed()
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
      }
    } finally {
      compose.activityRule.scenario.onActivity { it.setContent {}; owner.clear() }
      client.close()
      runBlocking { RecordCacheAccount.clear(context) }
      directory.deleteRecursively()
    }
  }
}
