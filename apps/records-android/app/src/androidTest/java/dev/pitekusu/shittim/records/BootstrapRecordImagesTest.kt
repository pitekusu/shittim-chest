package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import androidx.activity.compose.LocalActivityResultRegistryOwner
import androidx.activity.compose.setContent
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.auth.StoredToken
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordImageStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import io.ktor.client.engine.mock.MockEngine
import java.io.File
import java.nio.file.Files
import java.time.Instant
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class BootstrapRecordImagesTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun openingAndReopeningSavedRecordPreservesOriginalsBeforeDetailBecomesReady() {
    val app = compose.activity.applicationContext
    val directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "record-images-visit-").toFile()
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
    // Restored cache authorization is valid while the foreground check is pending.
    // The fixture never grants network access or needs a real Records/S3 request.
    val responseGate = CompletableDeferred<Unit>()
    val client = MobileAuthClient(MockEngine { responseGate.await(); error("unexpected_auth_response") })
    val image = DemoFixtures.sampleImages.first()
    val original = DemoFixtures.sampleImageBytes(image)
    try {
      runBlocking {
        RecordCacheAccount.activate(context, accountId)
        val keys = RecordDataKeyProtector(KeystorePrivateKeyStore(context))
        val database = EncryptedRecordsDatabase.open(context)
        try {
          val entry = RecordListEntry(recordId, "架空の画像付き議題", "架空の依頼者",
            RecordAvatar(null, "cyan"), now, "アロナ")
          val preview = RecordPreview("架空の画像付き議題", "架空の結論", "アロナ", attachments = listOf(image))
          val cache = EncryptedRecordStore(database, keys)
          for ((part, value) in listOf(
            CachedRecordPart.LIST to """{"schemaVersion":1,"entry":${Json.encodeToString(entry)}}""",
            CachedRecordPart.DETAIL to """{"schemaVersion":1,"preview":${Json.encodeToString(preview)}}""")) {
            val payload = value.toByteArray()
            try { cache.save(accountId, recordId, part, payload) }
            finally { payload.fill(0) }
          }
          EncryptedRecordImageStore(context, keys).save(accountId, recordId, image, original)
        } finally { database.close() }
      }
      var rendered: BootstrapScreen.State? = null
      compose.activityRule.scenario.onActivity { activity ->
        val model = MobileSessionModel(client, { stored }, { stored = it }, { stored = null }, {},
          {}, { false }, {}, { RecordCacheAccount.activate(context, it) }, { RecordCacheAccount.clear(context) })
        owner.put("session", model)
        val presenter = BootstrapPresenter(model)
        activity.setContent {
          CompositionLocalProvider(LocalContext provides context, LocalActivityResultRegistryOwner provides activity) {
            rendered = presenter.present()
          }
        }
      }
      compose.waitUntil(10_000) {
        rendered?.canReadRecords == true &&
          (rendered?.records as? RecordListState.Ready)?.loadedIds?.contains(recordId) == true
      }
      repeat(3) {
        compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.OpenRecord(recordId)) }
        compose.waitUntil(10_000) {
          (rendered?.record as? RecordPreviewState.Ready)?.images?.singleOrNull()?.let { !it.loading } == true
        }
        compose.runOnIdle {
          val loaded = (rendered!!.record as RecordPreviewState.Ready).images.single()
          assertNull(loaded.failure)
          assertArrayEquals(original, loaded.bytes)
          assertFalse(responseGate.isCompleted)
        }
        // A transient Idle/Loading state on the next visit must not prune the saved file.
        compose.runOnIdle { rendered!!.eventSink(BootstrapScreen.Event.CloseRecord) }
        compose.waitUntil(5_000) { rendered?.selectedRecordId == null }
        runBlocking {
          assertArrayEquals(original, EncryptedRecordImageStore(context).read(accountId, recordId, image))
        }
      }
    } finally {
      compose.activityRule.scenario.onActivity { it.setContent {}; owner.clear() }
      responseGate.cancel()
      client.close()
      original.fill(0)
      runBlocking { RecordCacheAccount.clear(context) }
      directory.deleteRecursively()
    }
  }
}
