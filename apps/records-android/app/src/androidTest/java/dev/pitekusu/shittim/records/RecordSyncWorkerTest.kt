package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import androidx.test.platform.app.InstrumentationRegistry
import androidx.work.ListenableWorker
import androidx.work.WorkerFactory
import androidx.work.WorkerParameters
import androidx.work.testing.TestListenableWorkerBuilder
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionResponse
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.StoredToken
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.headersOf
import java.io.File
import java.nio.file.Files
import java.time.Clock
import java.time.Instant
import java.time.ZoneOffset
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test

class RecordSyncWorkerTest {
  private lateinit var directory: File
  private lateinit var context: Context
  private lateinit var store: KeystoreTokenStore
  private val now = Instant.now().minusSeconds(1).let { Instant.ofEpochSecond(it.epochSecond) }
  private val deadline = now.plusSeconds(3600)
  private val token = StoredToken("t".repeat(43), deadline, CacheAuthorization("u".repeat(43), now, deadline))

  @Before fun setUp() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "worker-test-").toFile()
    context = object : ContextWrapper(app) {
      override fun getApplicationContext(): Context = this
      override fun getNoBackupFilesDir() = directory
      override fun getPackageName() = "${app.packageName}.worker.${directory.name}"
      override fun getDatabasePath(name: String): File = File(directory, name)
    }
    store = KeystoreTokenStore(context)
  }

  @After fun tearDown() {
    store.clear()
    store.completeLogout()
    KeystorePrivateKeyStore(context).clear()
    directory.deleteRecursively()
  }

  @Test fun validWorkerCompletesSyncWithoutPersistingCredentialsInWorkData() = runBlocking {
    store.save(token)
    RecordCacheAccount.activate(context, "u".repeat(43))
    var calls = 0
    val factory = object : WorkerFactory() {
      override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker =
        RecordSyncWorker(context, workerParameters, store,
          { MobileSessionResponse(1, "u".repeat(43),
            MobileSessionUser("架空の依頼者", MobileAvatar("placeholder", "依頼者", "cyan")), false, deadline) },
          { lease ->
            val database = EncryptedRecordsDatabase.open(context)
            val keys = KeystorePrivateKeyStore(context)
            RecordsRepository(RecordsReadClient(MockEngine { request ->
              calls++
              assertEquals("/api/v1/records/sync-index", request.url.encodedPath)
              respond("""{"schemaVersion":1,"items":[],"nextCursor":null}""",
                headers = headersOf(HttpHeaders.ContentType, "application/json"))
            }), EncryptedRecordStore(database, RecordDataKeyProtector(keys)), database,
              RecordCacheAccount(context, keys, database), lease::permits, activateOwner = false)
          })
    }
    val result = TestListenableWorkerBuilder<RecordSyncWorker>(context).setWorkerFactory(factory).build().doWork()
    assertTrue("worker_failure=${(result as? ListenableWorker.Result.Failure)?.outputData?.getString("failure")}",
      result is ListenableWorker.Result.Success)
    result as ListenableWorker.Result.Success
    assertEquals(1, calls)
    assertEquals(setOf("finishedAt"), result.outputData.keyValueMap.keys)
    assertEquals(token.cacheAuthorization?.accountId, store.read()?.cacheAuthorization?.accountId)
    assertEquals(token.cacheAuthorization?.expiresAt, store.read()?.cacheAuthorization?.expiresAt)
  }

  @Test fun leaseRejectsExpiryLogoutAndAccountSwitchWithoutExtendingThePermit() {
    store.save(token)
    val lease = RecordSyncLease(store, token, deadline, Clock.fixed(now, ZoneOffset.UTC))
    assertTrue(lease.permits("u".repeat(43)))
    assertFalse(RecordSyncLease(store, token, deadline, Clock.fixed(deadline, ZoneOffset.UTC)).permits("u".repeat(43)))
    val next = StoredToken("n".repeat(43), deadline, CacheAuthorization("v".repeat(43), now, deadline))
    store.save(next)
    assertFalse(lease.permits("u".repeat(43)))
    store.invalidateCacheAuthorization(token.accessToken) // A late failure cannot lock the new login.
    assertNotNull(store.read()?.cacheAuthorization)
    store.beginLogout()
    assertFalse(RecordSyncLease(store, next, deadline, Clock.fixed(now, ZoneOffset.UTC)).permits("v".repeat(43)))
  }

  @Test fun workerStopsBeforeNetworkWhenNoValidPermitOrDeletionIsPending() = runBlocking {
    val factory = object : WorkerFactory() {
      override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker =
        RecordSyncWorker(context, workerParameters, store,
          { error("must_not_verify_an_unauthorized_session") },
          { error("must_not_open_unauthorized_cache") })
    }
    for (pending in listOf(false, true)) {
      if (pending) { store.save(token); store.beginLogout() }
      val worker = TestListenableWorkerBuilder<RecordSyncWorker>(context).setWorkerFactory(factory).build()
      val result = worker.doWork() as ListenableWorker.Result.Failure
      assertEquals("AUTH_REQUIRED", result.outputData.getString("failure"))
      assertEquals(setOf("failure", "finishedAt"), result.outputData.keyValueMap.keys)
    }
  }

  @Test fun wrongServerIdentityLocksThePersistedPermitWithoutDeletingRecords() = runBlocking {
    store.save(token)
    val factory = object : WorkerFactory() {
      override fun createWorker(appContext: Context, workerClassName: String, workerParameters: WorkerParameters): ListenableWorker =
        RecordSyncWorker(context, workerParameters, store,
          { MobileSessionResponse(1, "v".repeat(43),
            MobileSessionUser("架空の依頼者", MobileAvatar("placeholder", "依頼者", "cyan")), false, deadline) },
          { error("wrong_identity_must_not_open_cache") })
    }
    val result = TestListenableWorkerBuilder<RecordSyncWorker>(context).setWorkerFactory(factory).build()
      .doWork() as ListenableWorker.Result.Failure
    assertEquals("AUTH_REQUIRED", result.outputData.getString("failure"))
    assertNull(store.read()?.cacheAuthorization)
    assertEquals(token.accessToken, store.read()?.accessToken)
  }
}
