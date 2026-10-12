package dev.pitekusu.shittim.records

import android.app.Application
import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.MobileLoginStatus
import dev.pitekusu.shittim.records.auth.MobileLoginStep
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.auth.StoredToken
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import dev.pitekusu.shittim.records.storage.StorageNamespace
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.File
import java.nio.file.Files
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import java.time.temporal.ChronoUnit
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ReviewDemoAppModelTest {
  private lateinit var app: Application
  private lateinit var directory: File
  private lateinit var modes: ReviewDemoModeStore
  private val owners = mutableListOf<ViewModelStore>()
  private val normalAccount = "n".repeat(43)
  private val normalRecord = "r".repeat(43)
  private val now = Instant.now().truncatedTo(ChronoUnit.SECONDS)
  private val authCalls = AtomicInteger()
  private var originallyRevoked = false

  @Before fun prepare() {
    val base = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(base.noBackupFilesDir.toPath(), "review-app-model-test-").toFile()
    val prefix = directory.name
    app = object : Application() {
      init { attachBaseContext(base) }
      override fun getApplicationContext(): Context = this
      override fun getNoBackupFilesDir(): File = directory
      override fun getDatabasePath(name: String): File = File(directory, name)
      override fun getPackageName(): String = "${base.packageName}.$prefix"
      override fun getSharedPreferences(name: String, mode: Int) =
        base.getSharedPreferences("$prefix-$name", mode)
    }
    modes = ReviewDemoModeStore(app)
    synchronized(RecordNotifications.lock) { originallyRevoked = RecordNotifications.locallyRevoked }
  }

  @After fun cleanup() = runBlocking {
    withContext(Dispatchers.Main) { owners.forEach(ViewModelStore::clear) }
    RecordSyncScheduler.cancelAndAwait(app)
    RecordNotificationRegistration.cancelAndAwait(app)
    RecordCacheAccount.clear(app)
    RecordCacheAccount.clear(app, StorageNamespace.DEMO)
    KeystoreTokenStore(app).let { it.clear(); it.completeLogout() }
    for (name in listOf("record-notification-v1", "review-demo-notifications-v1"))
      app.getSharedPreferences(name, Context.MODE_PRIVATE).edit().clear().commit()
    synchronized(RecordNotifications.lock) { RecordNotifications.locallyRevoked = originallyRevoked }
    directory.deleteRecursively()
    Unit
  }

  @Test fun coldDemoRestoreNeverConstructsAuthenticationAndResetInvalidatesOldIntents() = runBlocking {
    val entry = UUID.randomUUID().toString()
    modes.write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    val constructions = AtomicInteger()
    val model = start { constructions.incrementAndGet(); error("demo_must_not_construct_authentication") }
    val first = ready(model, demo = true)
    assertNull(first.session)
    assertEquals(entry, first.demoEntryId)
    val record = DemoFixtures.records.first().entry.recordId
    for (intent in listOf(
        demoNotificationIntent(app, UUID.randomUUID().toString(), record),
        demoNotificationIntent(app, entry, "x".repeat(43)),
        Intent(REVIEW_DEMO_NOTIFICATION_ACTION).putExtra(REVIEW_DEMO_NOTIFICATION_ENTRY, 17),
        Intent(Intent.ACTION_VIEW, Uri.parse("https://shittim.pitekusu.dev/records/$record")),
        Intent(Intent.ACTION_VIEW, Uri.parse("https://shittim.pitekusu.dev/auth/mobile/callback")))) {
      withContext(Dispatchers.Main) { model.openIntent(intent); model.onForeground() }
      assertNull(first.demoDestination.value)
      assertEquals(ReviewDemoMode(ReviewDemoPhase.DEMO, entry), modes.read())
    }
    withContext(Dispatchers.Main) { model.openIntent(demoNotificationIntent(app, entry, record)) }
    assertEquals(record, withTimeout(5_000) { first.demoDestination.first { it != null } })
    assertTrue(first.consumeDemoRecord(record))
    assertFalse(first.consumeDemoRecord(record))
    requireNotNull(first.demo).save(DebateWorkspace(draft = "消去される架空の下書き"))
    val oldExit = first.exitDemo
    val oldReset = first.resetDemo
    withContext(Dispatchers.Main) { first.resetDemo() }
    val reset = ready(model, demo = true, previous = first)
    assertNotEquals(entry, reset.demoEntryId)
    assertFalse(first.isActive(DEMO_ACCOUNT_ID))
    assertFalse(modes.isDemo(entry))
    assertEquals("", requireNotNull(reset.demo).load().draft)
    withContext(Dispatchers.Main) { model.openIntent(demoNotificationIntent(app, entry, record)) }
    assertNull(reset.demoDestination.value)
    val nextEntry = requireNotNull(reset.demoEntryId)
    val nextDemo = requireNotNull(reset.demo)
    val retainedDraft = "新しいデモに保持する架空の下書き"
    nextDemo.save(DebateWorkspace(draft = retainedDraft))
    for (staleAction in listOf(oldExit, oldReset)) {
      withContext(Dispatchers.Main) { staleAction() }
      assertSame(reset, (model.state.value as? RecordsAppState.Ready)?.runtime)
      assertEquals(ReviewDemoMode(ReviewDemoPhase.DEMO, nextEntry), modes.read())
      assertEquals(retainedDraft, nextDemo.load().draft)
    }
    assertEquals(0, constructions.get())
  }

  @Test fun demoEntryErasesNormalCacheForValidExpiredMissingAndCorruptCredentialsAndExitErasesDemo() = runBlocking {
    for (credential in listOf("valid", "expired", "missing", "corrupt")) {
      val callsBefore = authCalls.get()
      modes.write(ReviewDemoMode(ReviewDemoPhase.NORMAL))
      seedStorage(StorageNamespace.PRODUCTION, normalAccount)
      val store = KeystoreTokenStore(app)
      if (credential != "missing") {
        val expires = if (credential == "expired") now.minusSeconds(60) else now.plusSeconds(3600)
        store.save(StoredToken("t".repeat(43), expires,
          CacheAuthorization(normalAccount, now.minusSeconds(120), expires)))
        if (credential == "corrupt") {
          val file = File(directory, "mobile-session.v1")
          file.writeBytes(file.readBytes().also { it[it.lastIndex] = (it.last().toInt() xor 1).toByte() })
        }
      }
      val model = start(::fakeSession)
      val normal = ready(model, demo = false)
      val session = requireNotNull(normal.session)
      withTimeout(5_000) { session.state.first {
        when (credential) {
          "valid" -> it is SessionState.SignedIn
          "corrupt" -> it == SessionState.StorageError
          else -> it is SessionState.SignedOut
        }
      } }
      assertTrue(File(directory, "records-private-key.v1").exists())
      withContext(Dispatchers.Main) { normal.enterDemo() }
      val demo = ready(model, demo = true, previous = normal)
      assertStorageErased(StorageNamespace.PRODUCTION)
      assertNull(store.read())
      assertFalse(store.isLogoutPending())
      assertNull(demo.session)
      val callsAfterTransition = authCalls.get()
      if (credential == "corrupt") assertEquals(callsBefore, callsAfterTransition)
      val sandbox = requireNotNull(demo.demo)
      val image = sandbox.importImage(DEMO_SAMPLE_IMAGE_URI)
      sandbox.save(DebateWorkspace(draft = "架空のデモ下書き", attachments = listOf(image)))
      assertTrue(File(directory, "review-demo-records-private-key.v1").exists())
      withContext(Dispatchers.Main) { model.onForeground(); demo.exitDemo() }
      val after = ready(model, demo = false, previous = demo)
      withTimeout(5_000) { requireNotNull(after.session).state.first { it is SessionState.SignedOut } }
      assertStorageErased(StorageNamespace.DEMO)
      assertStorageErased(StorageNamespace.PRODUCTION)
      assertEquals(callsAfterTransition, authCalls.get())
      withContext(Dispatchers.Main) { release(model) }
    }
  }

  @Test fun interruptedEnteringExitingAndResettingFinishCleanupBeforeAuthenticationConstruction() = runBlocking {
    for (phase in listOf(ReviewDemoPhase.ENTERING, ReviewDemoPhase.EXITING, ReviewDemoPhase.RESETTING)) {
      val entry = UUID.randomUUID().toString()
      val namespace = if (phase == ReviewDemoPhase.ENTERING) StorageNamespace.PRODUCTION else StorageNamespace.DEMO
      seedStorage(namespace, if (namespace == StorageNamespace.PRODUCTION) normalAccount else DEMO_ACCOUNT_ID)
      File(directory, namespace.name("records-cache-delete.v1")).writeBytes(byteArrayOf(1))
      if (phase == ReviewDemoPhase.ENTERING) KeystoreTokenStore(app).beginLogout()
      modes.write(ReviewDemoMode(phase, entry.takeUnless { phase == ReviewDemoPhase.ENTERING }))
      val constructions = AtomicInteger()
      val model = start { context ->
        check(modes.read().phase == ReviewDemoPhase.NORMAL) { "authentication_before_cleanup" }
        assertStorageErased(namespace)
        constructions.incrementAndGet()
        fakeSession(context)
      }
      val runtime = ready(model, demo = phase != ReviewDemoPhase.EXITING)
      assertStorageErased(namespace, expectOwner = namespace == StorageNamespace.DEMO && phase == ReviewDemoPhase.RESETTING)
      assertFalse(KeystoreTokenStore(app).isLogoutPending())
      if (phase == ReviewDemoPhase.EXITING) {
        assertEquals(1, constructions.get())
        assertEquals(ReviewDemoPhase.NORMAL, modes.read().phase)
      } else {
        assertEquals(0, constructions.get())
        assertNotEquals(entry, runtime.demoEntryId)
        assertNotNull(runtime.demo)
      }
      withContext(Dispatchers.Main) { release(model) }
      RecordCacheAccount.clear(app, StorageNamespace.DEMO)
    }
    assertEquals(0, authCalls.get())
  }

  @Test fun ownedControllerCompletesALocalRequestWithoutAVisibleProgressScreen() = runBlocking {
    val entry = UUID.randomUUID().toString()
    modes.write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    val model = start { error("local_driver_must_not_construct_authentication") }
    val runtime = ready(model, demo = true)
    val sandbox = requireNotNull(runtime.demo)
    val submission = withContext(Dispatchers.Main) {
      DebateSubmissionModel({ runtime.isActive(DEMO_ACCOUNT_ID) }, sandbox::load, sandbox::save,
        { id, question -> sandbox.submit(id, question) }, sandbox::find, {}, sandbox::list,
        importImage = sandbox::importImage, readImage = sandbox::readImage, sendWithImages = sandbox::submit)
        .also { runtime.viewModelStore.put("debate", it); it.restore() }
    }
    withTimeout(5_000) { submission.state.first { it.workspace != null } }
    val question = "架空の背景進行確認：ピクニックを計画してください。"
    withContext(Dispatchers.Main) { submission.edit(question); submission.addImages(listOf(DEMO_SAMPLE_IMAGE_URI)) }
    withTimeout(5_000) { submission.state.first { !it.busy && it.workspace?.attachments?.size == 1 } }
    withContext(Dispatchers.Main) { submission.submit() }
    val submitted = withTimeout(5_000) { submission.state.first { !it.busy && it.workspace?.receipt != null } }
    val id = requireNotNull(submitted.workspace?.requestId)
    val submittedGeneration = sandbox.changes.value
    // Observe only a publication signal; calling find/list here would advance the request manually.
    withTimeout(20_000) { sandbox.changes.first { it > submittedGeneration } }
    val receipt = requireNotNull(sandbox.find(id))
    assertEquals("published", receipt.status)
    assertEquals(7, sandbox.cachedRecords(DEMO_ACCOUNT_ID).size)
    val recordId = requireNotNull(receipt.recordId)
    val detail = requireNotNull(sandbox.cachedRecord(DEMO_ACCOUNT_ID, recordId))
    assertEquals(question, detail.question)
    val bytes = requireNotNull(sandbox.cachedAttachment(DEMO_ACCOUNT_ID, recordId, detail.attachments.single()))
    try { validateAttachmentBytes(bytes, detail.attachments.single()) } finally { bytes.fill(0) }
    withContext(Dispatchers.Main) { release(model) }
    assertNull(submission.state.value.workspace)
    assertTrue(submission.images.value.isEmpty())
    assertFalse(runtime.isActive(DEMO_ACCOUNT_ID))
    val restarted = start { error("restored_demo_must_not_construct_authentication") }
    val restored = requireNotNull(ready(restarted, demo = true).demo)
    assertEquals("published", restored.find(id)?.status)
    assertEquals(id, restored.load().requestId)
    assertEquals(0, authCalls.get())
  }

  @Test fun retryAfterAModeWriteFailureStopsTheOldDemoAndItsOwnedModels() = runBlocking {
    val entry = UUID.randomUUID().toString()
    modes.write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    val model = start { error("demo_retry_must_not_construct_authentication") }
    val previous = ready(model, demo = true)
    val oldDemo = requireNotNull(previous.demo)
    val draft = "架空の消去失敗回復確認"
    val child = withContext(Dispatchers.Main) {
      DebateSubmissionModel({ previous.isActive(DEMO_ACCOUNT_ID) }, oldDemo::load, oldDemo::save,
        { id, question -> oldDemo.submit(id, question) }, oldDemo::find, {}, oldDemo::list,
        importImage = oldDemo::importImage, readImage = oldDemo::readImage, sendWithImages = oldDemo::submit)
        .also { previous.viewModelStore.put("debate", it); it.restore() }
    }
    withTimeout(5_000) { child.state.first { it.workspace != null } }
    withContext(Dispatchers.Main) { child.edit(draft) }
    withTimeout(5_000) { child.state.first { it.workspace?.draft == draft && !it.saving } }

    // A nonempty directory blocks AtomicFile's next write while its committed DEMO remains readable.
    val interrupted = File(directory, "review-demo-mode.v1.new")
    assertTrue(interrupted.mkdir())
    val blocker = File(interrupted, "write-blocker").apply { writeBytes(byteArrayOf(1)) }
    withContext(Dispatchers.Main) { model.exitDemo() }
    withTimeout(5_000) { model.state.first { it == RecordsAppState.StorageError } }
    assertEquals(ReviewDemoMode(ReviewDemoPhase.DEMO, entry), modes.read())
    assertTrue(previous.isActive(DEMO_ACCOUNT_ID))
    assertTrue(blocker.delete())
    assertTrue(interrupted.delete())

    withContext(Dispatchers.Main) { model.recover() }
    val restored = ready(model, demo = true, previous = previous)
    assertEquals(entry, restored.demoEntryId)
    assertNotSame(oldDemo, restored.demo)
    assertFalse(previous.isActive(DEMO_ACCOUNT_ID))
    assertFalse(oldDemo.isActive())
    assertNull(child.state.value.workspace)
    assertTrue(child.images.value.isEmpty())
    // The unsuccessful exit never committed, so only the new runtime may restore the saved draft.
    assertEquals(draft, requireNotNull(restored.demo).load().draft)
    assertEquals(0, authCalls.get())
  }

  @Test fun demoNotificationAndWrongAppLinksCannotSwitchANormalSessionOrGrantRecordAccess() = runBlocking {
    val model = start(::fakeSession)
    val normal = ready(model, demo = false)
    val session = requireNotNull(normal.session)
    withTimeout(5_000) { session.state.first { it is SessionState.SignedOut } }
    for (intent in listOf(
        demoNotificationIntent(app, UUID.randomUUID().toString(), DemoFixtures.records.first().entry.recordId),
        Intent(Intent.ACTION_VIEW, Uri.parse("https://example.invalid/records/$normalRecord")),
        Intent(Intent.ACTION_VIEW, Uri.parse("https://shittim.pitekusu.dev/auth/mobile/callback")))) {
      withContext(Dispatchers.Main) { model.openIntent(intent) }
      assertEquals(ReviewDemoPhase.NORMAL, modes.read().phase)
      assertNull(normal.demoDestination.value)
      assertNull(session.pendingDestination.value)
    }
    withContext(Dispatchers.Main) {
      model.openIntent(Intent(Intent.ACTION_VIEW, Uri.parse("https://shittim.pitekusu.dev/records/$normalRecord")))
    }
    assertEquals("/records/$normalRecord", session.pendingDestination.value?.returnTo)
    assertNull(session.offlineCacheAccountId)
    assertFalse(normal.isActive(normalAccount))
    assertEquals(0, authCalls.get())
  }

  @Test fun retainedNormalEntryCallbackCannotEraseANewAuthenticatedSessionAfterADemoRoundTrip() = runBlocking {
    val model = start(::fakeSession)
    val first = ready(model, demo = false)
    val oldEnter = first.enterDemo
    withContext(Dispatchers.Main) { first.enterDemo() }
    val demo = ready(model, demo = true, previous = first)
    withContext(Dispatchers.Main) { demo.exitDemo() }
    val current = ready(model, demo = false, previous = demo)
    val session = requireNotNull(current.session)
    withTimeout(5_000) { session.state.first { it is SessionState.SignedOut } }
    seedStorage(StorageNamespace.PRODUCTION, normalAccount)
    val store = KeystoreTokenStore(app)
    store.save(StoredToken("t".repeat(43), now.plusSeconds(3600),
      CacheAuthorization(normalAccount, now.minusSeconds(60), now.plusSeconds(3600))))
    withContext(Dispatchers.Main) {
      assertTrue(session.beginLogin())
      session.loginResult(MobileLoginStep.Finished(MobileLoginStatus.SIGNED_IN))
    }
    withTimeout(5_000) { session.state.first { it is SessionState.SignedIn } }
    val callsBefore = authCalls.get()
    assertFalse(first.isActive(normalAccount))
    assertTrue(current.isActive(normalAccount))
    withContext(Dispatchers.Main) { oldEnter() }
    assertSame(current, (model.state.value as? RecordsAppState.Ready)?.runtime)
    assertEquals(ReviewDemoMode(ReviewDemoPhase.NORMAL), modes.read())
    assertTrue(session.state.value is SessionState.SignedIn)
    assertNotNull(store.read())
    DebateWorkspaceStore.open(app, normalAccount, current::isActive).use { workspace ->
      assertEquals("架空の隔離確認下書き", workspace.load(normalAccount).draft)
      assertEquals(1, workspace.load(normalAccount).attachments.size)
    }
    assertEquals(callsBefore, authCalls.get())
  }

  private suspend fun start(factory: (Context) -> MobileSessionModel): RecordsAppModel = withContext(Dispatchers.Main) {
    val owner = ViewModelStore().also(owners::add)
    RecordsAppModel(app, factory).also { owner.put("controller", it) }
  }

  private fun release(model: RecordsAppModel) {
    val owner = owners.first { it["controller"] === model }
    owner.clear()
    owners.remove(owner)
  }

  private suspend fun ready(model: RecordsAppModel, demo: Boolean,
    previous: RecordsRuntime? = null): RecordsRuntime = withTimeout(10_000) {
    val state = model.state.first {
      if (it == RecordsAppState.StorageError) error("controller_storage_error")
      it is RecordsAppState.Ready && it.runtime !== previous && (it.runtime.demo != null) == demo
    } as RecordsAppState.Ready
    state.runtime
  }

  private fun fakeSession(context: Context): MobileSessionModel {
    val store = KeystoreTokenStore(context)
    val client = MobileAuthClient(MockEngine { request ->
      authCalls.incrementAndGet()
      when {
        request.method == HttpMethod.Get && request.url.encodedPath.endsWith("/session") -> respond(
          """{"schemaVersion":1,"cacheAccountId":"$normalAccount","isAdmin":false,"expiresAt":"${now.plusSeconds(3600)}",
            "user":{"displayName":"架空の通常利用者","avatar":{"kind":"placeholder","alt":"架空","fallbackVariant":"cyan"}}}""",
          headers = headersOf(HttpHeaders.ContentType, "application/json"))
        request.method == HttpMethod.Post && request.url.encodedPath.endsWith("/logout") ->
          respond("", HttpStatusCode.NoContent)
        else -> error("unexpected_mock_auth_route")
      }
    })
    return MobileSessionModel(client, store::read, store::save, store::clear,
      store::invalidateCacheAuthorization, store::beginLogout, store::isLogoutPending, store::completeLogout,
      { RecordCacheAccount.activate(context, it) },
      { RecordCacheAccount.clear(context) }, clock = Clock.fixed(now, ZoneOffset.UTC))
  }

  private suspend fun seedStorage(namespace: StorageNamespace, account: String) {
    RecordCacheAccount.activate(app, account, namespace)
    val database = EncryptedRecordsDatabase.open(app, namespace)
    try {
      val keys = KeystorePrivateKeyStore(app, namespace)
      EncryptedRecordStore(database, RecordDataKeyProtector(keys)).save(account, normalRecord,
        CachedRecordPart.DETAIL, "架空の隔離確認本文".encodeToByteArray())
    } finally { database.close() }
    DebateWorkspaceStore.open(app, account, { true }, namespace).use { workspace ->
      val bytes = DemoFixtures.sampleImageBytes()
      val image = try { workspace.importBytes(account, bytes) } finally { bytes.fill(0) }
      workspace.save(account, DebateWorkspace(draft = "架空の隔離確認下書き", attachments = listOf(image)))
    }
  }

  private fun assertStorageErased(namespace: StorageNamespace, expectOwner: Boolean = false) {
    assertFalse(File(directory, namespace.name("records-private-key.v1")).exists())
    assertFalse(File(directory, namespace.name("record-images-v1")).exists())
    assertFalse(File(directory, namespace.name("records-cache-delete.v1")).exists())
    assertEquals(expectOwner, File(directory, namespace.name("records-cache-account.v1")).exists())
    val database = EncryptedRecordsDatabase.open(app, namespace)
    try {
      // Database queries are suspend operations; this helper is also used by the Main-only factory spy.
      runBlocking(Dispatchers.IO) {
        val account = if (namespace == StorageNamespace.PRODUCTION) normalAccount else DEMO_ACCOUNT_ID
        val cache = EncryptedRecordStore(database, RecordDataKeyProtector(KeystorePrivateKeyStore(app, namespace)))
        assertTrue(cache.rows(account, CachedRecordPart.DETAIL).isEmpty())
        assertTrue(cache.rows(account, CachedRecordPart.DEBATE_WORKSPACE).isEmpty())
      }
    } finally { database.close() }
  }
}
