package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import android.graphics.Bitmap
import android.graphics.Color
import java.io.ByteArrayOutputStream
import androidx.room3.Room
import androidx.sqlite.driver.AndroidSQLiteDriver
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import dev.pitekusu.shittim.records.storage.RecordCacheException
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.UUID
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.sync.withLock
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordsRepositoryTest {
  private val recordId = "r".repeat(43)
  private val accountId = "u".repeat(43)
  private val token = "t".repeat(43)
  private lateinit var app: Context
  private lateinit var privateDirectory: File
  private lateinit var privateContext: Context
  private lateinit var privateKeys: KeystorePrivateKeyStore
  private lateinit var databaseName: String
  private var activeAccountId = accountId

  @Before fun setUp() {
    app = InstrumentationRegistry.getInstrumentation().targetContext
    privateDirectory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "repo-test-").toFile()
    privateContext = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir(): File = privateDirectory
      override fun getPackageName(): String = "${app.packageName}.test.${UUID.randomUUID()}"
    }
    privateKeys = KeystorePrivateKeyStore(privateContext)
    activeAccountId = accountId
    databaseName = "repo-test-${UUID.randomUUID()}.db"
  }

  @After fun tearDown() {
    privateKeys.clear()
    app.deleteDatabase(databaseName)
    privateDirectory.deleteRecursively()
  }

  @Test fun onlineListAndDetailSurviveRepositoryReopen() = runBlocking {
    repository(MockEngine { request ->
      val body = if (request.url.encodedPath.endsWith("/$recordId")) detailWithReasons(recordId) else list()
      respond(body, headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { online ->
      assertEquals("架空の議題", online.recentRecords(token, accountId, null).items.single().questionPreview)
      assertEquals("架空の結論", (online.record(token, accountId, recordId) as RecordReadResult.Found)
        .preview.decision)
    }
    val marker = "架空の議題".toByteArray().toString(Charsets.ISO_8859_1)
    app.getDatabasePath(databaseName).parentFile!!.listFiles()!!
      .filter { it.name.startsWith(databaseName) && it.isFile }
      .forEach { file ->
        val contents = file.readBytes().toString(Charsets.ISO_8859_1)
        assertFalse(contents.contains(marker))
        assertFalse(contents.contains("架空の率直な感想".toByteArray().toString(Charsets.ISO_8859_1)))
      }
    repository(MockEngine { error("unexpected_network_request") }).use { reopened ->
      assertEquals("架空の議題", reopened.cachedListEntry(accountId, recordId)?.questionPreview)
      assertEquals("participant-a", reopened.cachedListEntry(accountId, recordId)?.winnerSlot)
      assertEquals("アロナ", reopened.cachedRecord(accountId, recordId)?.winnerName)
      assertEquals("架空の結論", reopened.cachedRecord(accountId, recordId)?.decision)
      val changes = reopened.cachedRecord(accountId, recordId)!!.affection!!.changes
      assertEquals("架空の率直な感想", changes[0].reason)
      assertEquals(RecordAffectionReasonStatus.AVAILABLE, changes[0].reasonStatus)
      assertEquals(RecordAffectionReasonStatus.UNAVAILABLE, changes[1].reasonStatus)
      assertEquals(-20, changes[1].appliedDelta)
      assertEquals(RecordAffectionReasonStatus.NOT_RECORDED, changes[2].reasonStatus)
    }
  }

  @Test fun olderEncryptedDetailWithoutReasonFieldsRemainsReadableOffline() = runBlocking {
    repository(MockEngine { respond(detail(), headers = headersOf(HttpHeaders.ContentType, "application/json")) })
      .use { it.record(token, accountId, recordId) }
    val legacy = """{"schemaVersion":1,"preview":{"question":"架空の議題","decision":"架空の結論",
      "winnerName":"アロナ","affection":{"status":"APPLIED","changes":[
        {"participantName":"アロナ","before":500,"questionScore":10,"appliedDelta":10,"after":510},
        {"participantName":"プラナ","before":500,"questionScore":-20,"appliedDelta":-20,"after":480},
        {"participantName":"安倍晋三AI","before":500,"questionScore":0,"appliedDelta":0,"after":500}]}}}"""
      .toByteArray()
    val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    try {
      RecordCacheAccount.lock.withLock {
        EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
          .save(accountId, recordId, CachedRecordPart.DETAIL, legacy)
      }
    } finally { legacy.fill(0); database.close() }
    repository(MockEngine { error("offline_must_not_request_network") }).use { records ->
      val changes = records.cachedRecord(accountId, recordId)!!.affection!!.changes
      assertEquals(listOf(10, -20, 0), changes.map { it.appliedDelta })
      assertTrue(changes.all { it.reason == null && it.reasonStatus == null })
    }
  }

  @Test fun anotherAccountReplacesTheOldKeyAndCiphertext() = runBlocking {
    val engine = { MockEngine { respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json")) } }
    repository(engine()).use { it.recentRecords(token, accountId, null) }
    val nextAccount = "v".repeat(43)
    activeAccountId = nextAccount
    repository(engine()).use { next ->
      assertEquals("架空の議題", next.recentRecords(token, nextAccount, null).items.single().questionPreview)
      assertEquals("架空の議題", next.cachedListEntry(nextAccount, recordId)?.questionPreview)
      try {
        next.cachedListEntry(accountId, recordId)
        fail("previous account must not regain access")
      } catch (error: RecordReadException) {
        assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure)
      }
    }
    val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    try {
      assertNull(EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
        .load(accountId, recordId, CachedRecordPart.LIST))
    } finally { database.close() }
  }

  @Test fun localSearchReadsEncryptedBodyWithoutNetworkAndHonorsFiltersAndPermission() = runBlocking {
    repository(MockEngine {
      respond(detail().replace("架空の結論", "Ｐｙｔｈｏｎで月の観測")
        .replace("\"displayName\":\"アロナ\"", "\"displayName\":\"Arona\""),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { it.record(token, accountId, recordId) }
    repository(MockEngine { error("search_must_not_request_network") }).use { saved ->
      val entries = saved.cachedRecords(accountId)
      assertEquals("participant-a", entries.single().winnerSlot)
      assertEquals(recordId, saved.queryCachedRecords(accountId, entries,
        RecordListQuery("python 観測")).single().recordId)
      assertEquals(recordId, saved.queryCachedRecords(accountId, entries,
        RecordListQuery("架空 観測", RecordWinner.Arona)).single().recordId)
      assertEquals(emptyList<RecordListEntry>(), saved.queryCachedRecords(accountId, entries,
        RecordListQuery("観測", RecordWinner.Plana)))
      assertEquals(emptyList<RecordListEntry>(), saved.queryCachedRecords(accountId, entries,
        RecordListQuery("存在しない語句")))
      assertEquals(recordId, saved.queryCachedRecords(accountId, entries, RecordListQuery("理由B"))
        .single().recordId)
      activeAccountId = ""
      try {
        saved.queryCachedRecords(accountId, entries, RecordListQuery("観測"))
        fail("revoked permission must not search cached body")
      } catch (error: RecordReadException) { assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure) }
    }
    val marker = "月の観測".toByteArray().toString(Charsets.ISO_8859_1)
    app.getDatabasePath(databaseName).parentFile!!.listFiles()!!
      .filter { it.name.startsWith(databaseName) && it.isFile }
      .forEach { assertFalse(it.readBytes().toString(Charsets.ISO_8859_1).contains(marker)) }
  }

  @Test fun syncCheckpointSurvivesReopenAndRejectsInvalidOrUnauthorizedState() = runBlocking {
    val checkpoint = RecordSyncCheckpoint(cursor = "synthetic_cursor.signature",
      pendingIds = listOf(recordId), pageLoaded = true)
    repository(MockEngine { error("unexpected_network_request") }).use { records ->
      assertNull(records.syncCheckpoint(accountId))
      records.saveSyncCheckpoint(accountId, checkpoint)
      try {
        records.saveSyncCheckpoint(accountId, checkpoint.copy(pageLoaded = false))
        fail("invalid progress must not replace the saved checkpoint")
      } catch (error: RecordReadException) {
        assertEquals(RecordReadFailure.STORAGE_UNAVAILABLE, error.failure)
      }
    }
    val marker = checkpoint.cursor!!
    app.getDatabasePath(databaseName).parentFile!!.listFiles()!!
      .filter { it.name.startsWith(databaseName) && it.isFile }
      .forEach { assertFalse(it.readBytes().toString(Charsets.ISO_8859_1).contains(marker)) }
    repository(MockEngine { error("unexpected_network_request") }).use { records ->
      assertEquals(checkpoint, records.syncCheckpoint(accountId))
      activeAccountId = ""
      try {
        records.syncCheckpoint(accountId)
        fail("expired authorization must not read progress")
      } catch (error: RecordReadException) {
        assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure)
      }
    }
    activeAccountId = "v".repeat(43)
    repository(MockEngine { error("unexpected_network_request") }).use { records ->
      assertNull(records.syncCheckpoint(activeAccountId))
    }
  }

  @Test fun previousAccountsLateResponseCannotDeleteTheNewCache() = runBlocking {
    val started = CompletableDeferred<Unit>()
    val release = CompletableDeferred<Unit>()
    val delayed = repository(MockEngine {
      started.complete(Unit)
      release.await()
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    })
    try {
      val pending = async {
        try {
          delayed.recentRecords(token, accountId, null)
          fail("old session result must be rejected")
          null
        } catch (error: RecordReadException) { error.failure }
      }
      started.await()
      val nextAccount = "v".repeat(43)
      activeAccountId = nextAccount
      repository(MockEngine {
        respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
      }).use { next ->
        next.recentRecords(token, nextAccount, null)
        release.complete(Unit)
        assertEquals(RecordReadFailure.AUTH_REQUIRED, pending.await())
        assertEquals("架空の議題", next.cachedListEntry(nextAccount, recordId)?.questionPreview)
      }
    } finally {
      release.complete(Unit)
      delayed.close()
    }
  }

  @Test fun syncWalksEveryPageSeriallyAndFetchesOnlyMissingDetails() = runBlocking {
    val a = "a".repeat(43)
    val b = "b".repeat(43)
    val c = "c".repeat(43)
    val calls = mutableListOf<String>()
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      val body = if (id == "sync-index") {
        val cursor = request.url.parameters["cursor"]
        calls.add("list:${cursor ?: "first"}")
        if (cursor == null) index(listOf(a, b), "next.signature") else index(listOf(b, c))
      } else { calls.add("detail:$id"); detail(id) }
      respond(body, headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      records.record(token, accountId, a)
      calls.clear()
      records.synchronize(token, accountId)
      assertEquals(listOf("list:first", "detail:$a", "detail:$b", "list:next.signature", "detail:$c"), calls)
      assertEquals(true, records.syncCheckpoint(accountId)?.complete)
      for (id in listOf(a, b, c)) assertEquals("架空の結論", records.cachedRecord(accountId, id)?.decision)
      calls.clear()
      records.synchronize(token, accountId) // A completed pass checks new list pages, not old details.
      assertEquals(listOf("list:first", "list:next.signature"), calls)
    }
  }

  @Test fun failedOrCancelledSyncResumesThePendingDetailAfterReopen() = runBlocking {
    val nextId = "b".repeat(43)
    for (cancel in listOf(false, true)) {
      val entered = CompletableDeferred<Unit>()
      val release = CompletableDeferred<Unit>()
      val first = repository(MockEngine { request ->
        val id = request.url.encodedPath.substringAfterLast('/')
        if (id == nextId) {
          entered.complete(Unit)
          if (cancel) release.await() else throw IOException("synthetic_failure")
        }
        respond(if (id == "sync-index") index(listOf(recordId, nextId)) else detail(id),
          headers = headersOf(HttpHeaders.ContentType, "application/json"))
      })
      try {
        if (cancel) {
          val job = launch { first.synchronize(token, accountId) }
          entered.await()
          job.cancelAndJoin()
        } else {
          try { first.synchronize(token, accountId); fail("network failure must stop sync") }
          catch (error: RecordReadException) { assertEquals(RecordReadFailure.UNAVAILABLE, error.failure) }
        }
        assertEquals(listOf(nextId), first.syncCheckpoint(accountId)?.pendingIds)
        assertEquals(false, first.syncCheckpoint(accountId)?.complete)
      } finally { first.close() }
      val resumed = mutableListOf<String>()
      repository(MockEngine { request ->
        val id = request.url.encodedPath.substringAfterLast('/')
        resumed.add(id)
        respond(if (id == "sync-index") index(listOf(recordId, nextId)) else detail(id),
          headers = headersOf(HttpHeaders.ContentType, "application/json"))
      }).use { records ->
        records.synchronize(token, accountId)
        assertEquals(listOf("sync-index", nextId), resumed)
        assertEquals(true, records.syncCheckpoint(accountId)?.complete)
      }
      val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
        .setDriver(AndroidSQLiteDriver()).build()
      try { RecordCacheAccount.lock.withLock { RecordCacheAccount(privateContext, privateKeys, database).clear() } }
      finally { database.close() }
    }
  }

  @Test fun reasonContractUpgradeRestartsEveryLegacyIndexPageAndResumesWithoutRefetching() = runBlocking {
    val ids = listOf("a".repeat(43), "b".repeat(43), "c".repeat(43))
    val references = ids.map { RecordSyncReference(it, "z".repeat(43), "i".repeat(43)) }
    for (wasComplete in listOf(false, true)) {
      repository(MockEngine { request ->
        respond(detailWithReasons(request.url.encodedPath.substringAfterLast('/'), legacy = true),
          headers = headersOf(HttpHeaders.ContentType, "application/json"))
      }).use { records ->
        for (reference in references) {
          records.record(token, accountId, reference.recordId)
          records.saveRevision(accountId, reference)
        }
        records.saveSyncCheckpoint(accountId, if (wasComplete) {
          RecordSyncCheckpoint(complete = true, pageLoaded = true, indexBased = true,
            committedRevisions = references.associate { it.recordId to it.revision }, removalCandidates = emptySet())
        } else {
          RecordSyncCheckpoint(cursor = "tail.signature", pageLoaded = true, indexBased = true,
            pendingIds = listOf(ids[2]), pendingReferences = listOf(references[2]),
            committedRevisions = references.take(2).associate { it.recordId to it.revision },
            pendingAvatars = setOf(ids[1]), needsAvatarPrune = true, removalCandidates = emptySet())
        })
      }
      var failMiddle = true
      val calls = mutableListOf<String>()
      fun engine() = MockEngine { request ->
        val id = request.url.encodedPath.substringAfterLast('/')
        val cursor = request.url.parameters["cursor"]
        calls += if (id == "sync-index") "index:${cursor ?: "head"}" else id
        if (id == ids[1] && failMiddle) throw IOException("synthetic_failure")
        val body = if (id != "sync-index") {
          assertEquals("affection-reasons-v1", request.url.parameters["contract"])
          detailWithReasons(id)
        } else when (cursor) {
          null -> index(listOf(ids[0]), "middle.signature")
          "middle.signature" -> index(listOf(ids[1]), "tail.signature")
          "tail.signature" -> index(listOf(ids[2]))
          else -> error("unexpected_cursor")
        }
        respond(body, headers = headersOf(HttpHeaders.ContentType, "application/json"))
      }
      repository(engine()).use { records ->
        try { records.synchronize(token, accountId); fail("upgrade remains resumable on failure") }
        catch (error: RecordReadException) { assertEquals(RecordReadFailure.UNAVAILABLE, error.failure) }
        assertEquals(listOf("index:head", ids[0], "index:middle.signature", ids[1]), calls)
        val checkpoint = records.syncCheckpoint(accountId)!!
        assertEquals(1, checkpoint.readContractVersion)
        assertEquals(setOf(ids[0]), checkpoint.committedRevisions.keys)
        if (!wasComplete) {
          assertTrue(ids[1] in checkpoint.pendingAvatars)
          assertTrue(checkpoint.needsAvatarPrune)
        }
        assertNull(records.cachedRecord(accountId, ids[1])!!.affection!!.changes.first().reasonStatus)
        assertEquals(-20, records.cachedRecord(accountId, ids[1])!!.affection!!.changes[1].appliedDelta)
      }
      failMiddle = false
      calls.clear()
      repository(engine()).use { records ->
        records.synchronize(token, accountId)
        assertEquals(listOf("index:head", ids[1], "index:tail.signature", ids[2]), calls)
        assertTrue(records.syncCheckpoint(accountId)!!.complete)
        assertEquals(1, records.syncCheckpoint(accountId)!!.readContractVersion)
        for (id in ids) assertEquals("架空の率直な感想",
          records.cachedRecord(accountId, id)!!.affection!!.changes.first().reason)
        calls.clear()
        records.synchronize(token, accountId)
        assertEquals(listOf("index:head", "index:middle.signature", "index:tail.signature"), calls)
      }
    }
  }

  @Test fun deltaFetchesOnlyNewOrChangedResultsAndPersistsSharedIconsOffline() = runBlocking {
    val a = "a".repeat(43)
    val b = "b".repeat(43)
    val c = "c".repeat(43)
    var changed = false
    val calls = mutableListOf<String>()
    val bitmap = Bitmap.createBitmap(32, 32, Bitmap.Config.ARGB_8888).apply { eraseColor(Color.CYAN) }
    val png = ByteArrayOutputStream().use { output ->
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)
      output.toByteArray()
    }
    bitmap.recycle()
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      calls.add(id)
      if (id == "avatar.png") {
        assertNull(request.headers[HttpHeaders.Authorization])
        respond(png, headers = headersOf(HttpHeaders.ContentType, "image/png"))
      } else {
        val response = if (id == "sync-index") {
          index(if (changed) listOf(a, b, c) else listOf(a, b)).let { body ->
            if (changed) body.replace("\"recordId\":\"$b\",\"revision\":\"${"z".repeat(43)}\"",
              "\"recordId\":\"$b\",\"revision\":\"${"y".repeat(43)}\"") else body
          }
        } else detail(id).replace("\"kind\":\"placeholder\",\"url\":null",
          "\"kind\":\"image\",\"url\":\"https://fixture.s3.ap-northeast-1.amazonaws.com/requesters/fake/avatar.png?signature=${if (changed) 2 else 1}\"")
          .let { if (changed && id == b) it.replace("架空の結論", "更新後の結論") else it }
        respond(response, headers = headersOf(HttpHeaders.ContentType, "application/json"))
      }
    }).use { records ->
      records.synchronize(token, accountId)
      assertEquals(1, calls.count { it == "avatar.png" })
      calls.clear()
      changed = true
      records.synchronize(token, accountId)
      assertEquals(listOf("sync-index", b, c), calls)
      assertEquals("更新後の結論", records.cachedRecord(accountId, b)?.decision)
      calls.clear()
      records.synchronize(token, accountId)
      assertEquals(listOf("sync-index"), calls)
    }
    repository(MockEngine { error("offline_must_not_request_network") }).use { records ->
      val entries = records.cachedRecords(accountId)
      assertEquals(3, entries.size)
      for (entry in entries) {
        assertNull(entry.requesterAvatar.url)
        org.junit.Assert.assertNotNull(entry.requesterAvatar.bytes)
      }
      activeAccountId = ""
      try { records.cachedRecords(accountId); fail("expired session must not read icons") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure) }
    }
  }

  @Test fun expiredCursorRestartsOnceWithoutDownloadingSavedDetailsAgain() = runBlocking {
    val calls = mutableListOf<String>()
    repository(MockEngine { request ->
      val cursor = request.url.parameters["cursor"]
      calls.add(cursor ?: "first")
      if (cursor != null) respond("""{"error":{"code":"CURSOR_INVALID"}}""",
        HttpStatusCode.BadRequest, headersOf(HttpHeaders.ContentType, "application/json"))
      else respond(index(cursor = "new.signature"), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      // Detail already exists, so the restart will enumerate it but must not request it again.
      repository(MockEngine { respond(detail(), headers = headersOf(HttpHeaders.ContentType, "application/json")) })
        .use { it.record(token, accountId, recordId) }
      records.saveSyncCheckpoint(accountId, RecordSyncCheckpoint(readContractVersion = 1,
        cursor = "old.signature", removalCandidates = emptySet(), indexBased = true,
        committedRevisions = mapOf(recordId to "z".repeat(43))))
      try { records.synchronize(token, accountId); fail("a second invalid cursor must stop sync") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.CURSOR_INVALID, error.failure) }
      assertEquals(listOf("first", "old.signature", "first", "new.signature"), calls)
      assertEquals("架空の結論", records.cachedRecord(accountId, recordId)?.decision)
    }
  }

  @Test fun unchangedPassCheckpointsPagesInsteadOfEveryRecordAfterReopen() = runBlocking {
    val ids = (1..12).map { it.toString().padEnd(43, 'r') }
    val responses = { MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      respond(if (id == "sync-index") index(ids) else detail(id),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    } }
    repository(responses()).use { it.synchronize(token, accountId) }
    AndroidSQLiteDriver().open(app.getDatabasePath(databaseName).path).use { connection ->
      for (sql in listOf(
        "CREATE TABLE checkpoint_writes (count INTEGER NOT NULL)",
        "INSERT INTO checkpoint_writes VALUES (0)",
        "CREATE TRIGGER count_checkpoint_write AFTER UPDATE ON encrypted_records WHEN NEW.part = 'sync-progress' BEGIN UPDATE checkpoint_writes SET count = count + 1; END",
      )) connection.prepare(sql).use { it.step() }
    }
    repository(MockEngine { request ->
      assertEquals("sync-index", request.url.encodedPath.substringAfterLast('/'))
      respond(index(ids), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      records.synchronize(token, accountId)
      assertEquals(ids.toSet(), records.syncCheckpoint(accountId)?.committedRevisions?.keys)
    }
    AndroidSQLiteDriver().open(app.getDatabasePath(databaseName).path).use { connection ->
      connection.prepare("SELECT count FROM checkpoint_writes").use { query ->
        query.step()
        assertTrue("unchanged records must not each rewrite the encrypted checkpoint", query.getLong(0) <= 4)
      }
    }
  }

  @Test fun resumedTailFetchesTheLatestHeadBeforeItsPendingRecord() = runBlocking {
    val older = "o".repeat(43)
    val newest = "n".repeat(43)
    repository(MockEngine { request ->
      if (request.url.encodedPath.endsWith("/$older")) throw IOException("synthetic_failure")
      respond(index(listOf(older), "tail.signature"),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      try { records.synchronize(token, accountId); fail("pending work must remain resumable") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.UNAVAILABLE, error.failure) }
    }
    val calls = mutableListOf<String>()
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      calls += if (id == "sync-index") "index:${request.url.parameters["cursor"] ?: "head"}" else id
      respond(if (id != "sync-index") detail(id)
        else if (request.url.parameters["cursor"] == null) index(listOf(newest), "different.signature")
        else index(emptyList()), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      records.synchronize(token, accountId)
      assertEquals(listOf("index:head", newest, older, "index:tail.signature"), calls)
      assertEquals(setOf(newest, older), records.cachedCompleteRecordIds(accountId))
    }
  }

  @Test fun aCheckpointWriteFailureStillNotifiesAboutTheSavedResult() = runBlocking {
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      respond(when (id) { "records" -> list(); "sync-index" -> index(); else -> detail(id) },
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      records.recentRecords(token, accountId, null)
      AndroidSQLiteDriver().open(app.getDatabasePath(databaseName).path).use { connection ->
        connection.prepare("CREATE TRIGGER reject_checkpoint AFTER INSERT ON encrypted_records WHEN NEW.part = 'sync-progress' AND EXISTS (SELECT 1 FROM encrypted_records WHERE part = 'detail') BEGIN SELECT RAISE(ABORT, 'synthetic_checkpoint_failure'); END")
          .use { it.step() }
        connection.prepare("CREATE TRIGGER reject_checkpoint_update AFTER UPDATE ON encrypted_records WHEN NEW.part = 'sync-progress' AND EXISTS (SELECT 1 FROM encrypted_records WHERE part = 'detail') BEGIN SELECT RAISE(ABORT, 'synthetic_checkpoint_failure'); END")
          .use { it.step() }
      }
      var notifications = 0
      try { records.synchronize(token, accountId) { notifications++ }; fail("checkpoint failure must be reported") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.STORAGE_UNAVAILABLE, error.failure) }
      assertEquals(1, notifications)
      assertEquals("架空の結論", records.cachedRecord(accountId, recordId)?.decision)
      assertEquals(listOf(recordId), records.syncCheckpoint(accountId)?.pendingIds)
    }
  }

  @Test fun newResultsArePublishedBeforeAnAvatarCompletesAndImageRetryKeepsBodies() = runBlocking {
    val ids = listOf(recordId, "n".repeat(43))
    var failImage = true
    val entered = CompletableDeferred<Unit>()
    val release = CompletableDeferred<Unit>()
    val calls = mutableListOf<String>()
    val bodyNotifications = mutableListOf<Set<String>>()
    val bitmap = Bitmap.createBitmap(8, 8, Bitmap.Config.ARGB_8888)
    val png = ByteArrayOutputStream().use { output ->
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)
      output.toByteArray()
    }
    bitmap.recycle()
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      calls += id
      if (id == "avatar.png") {
        if (failImage) {
          entered.complete(Unit)
          release.await()
          throw IOException("synthetic_failure")
        }
        respond(png, headers = headersOf(HttpHeaders.ContentType, "image/png"))
      } else respond(if (id == "sync-index") index(ids) else detail(id).replace(
        "\"kind\":\"placeholder\",\"url\":null",
        "\"kind\":\"image\",\"url\":\"https://fixture.s3.ap-northeast-1.amazonaws.com/requesters/fake/avatar.png\""),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      val pending = async {
        try { records.synchronize(token, accountId) { bodyNotifications += records.cachedCompleteRecordIds(accountId) }; null }
        catch (error: RecordReadException) { error.failure }
      }
      try {
        entered.await()
        assertEquals(ids.toSet(), records.cachedRecords(accountId).map { it.recordId }.toSet())
        assertEquals(ids.toSet(), bodyNotifications.last())
        assertEquals(listOf("sync-index", ids[0], ids[1], "avatar.png"), calls)
      } finally { release.complete(Unit) }
      assertEquals(RecordReadFailure.UNAVAILABLE, pending.await())
      assertEquals(ids.toSet(), records.syncCheckpoint(accountId)?.pendingAvatars)
      calls.clear()
      failImage = false
      records.synchronize(token, accountId)
      assertEquals(listOf("sync-index", "avatar.png"), calls)
      assertEquals(true, records.syncCheckpoint(accountId)?.complete)
      assertTrue(records.cachedRecords(accountId).all { it.requesterAvatar.bytes != null })
    }
  }

  @Test fun cyclicCursorsStopAndMissingDetailsDoNotBlockRemainingRecords() = runBlocking {
    var calls = 0
    repository(MockEngine {
      val next = listOf("a.signature", "b.signature", "a.signature")[calls++]
      respond(index(emptyList(), next), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      try { records.synchronize(token, accountId); fail("cursor cycle must not loop forever") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.INVALID_RESPONSE, error.failure) }
      assertEquals(3, calls)
    }
    val remaining = "c".repeat(43)
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      if (id == recordId) respond("", HttpStatusCode.NotFound)
      else respond(if (id == "sync-index") index(listOf(recordId, remaining)) else detail(id),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      records.saveSyncCheckpoint(accountId, RecordSyncCheckpoint())
      records.synchronize(token, accountId)
      assertNull(records.cachedRecord(accountId, recordId))
      assertEquals("架空の結論", records.cachedRecord(accountId, remaining)?.decision)
      assertEquals(true, records.syncCheckpoint(accountId)?.complete)
    }
  }

  @Test fun authorizationLossDuringSyncCannotSaveTheDelayedDetailOrAdvanceProgress() = runBlocking {
    repository(MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      if (id != "sync-index") activeAccountId = ""
      respond(if (id == "sync-index") index() else detail(),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      try { records.synchronize(token, accountId); fail("lost authorization must stop sync") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure) }
      activeAccountId = accountId
      assertNull(records.cachedRecord(accountId, recordId))
      assertEquals(listOf(recordId), records.syncCheckpoint(accountId)?.pendingIds)
    }
  }

  @Test fun failedSaveNeverReturnsOnlinePlaintext() = runBlocking {
    activeAccountId = "invalid account"
    repository(MockEngine { request ->
      respond(if (request.url.encodedPath.endsWith("/$recordId")) detail() else list(),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { repository ->
      for (request in listOf<suspend () -> Any>(
        { repository.recentRecords(token, "invalid account", null) },
        { repository.record(token, "invalid account", recordId) },
      )) {
        try {
          request()
          fail("online data must not be returned after a failed cache save")
        } catch (error: RecordReadException) {
          assertEquals(RecordReadFailure.STORAGE_UNAVAILABLE, error.failure)
        }
      }
    }
  }

  @Test fun completedEnumerationPrunesOnlyAbsentRecordsAndFailureKeepsThem() = runBlocking {
    val missing = "m".repeat(43)
    val engine = MockEngine { request ->
      val id = request.url.encodedPath.substringAfterLast('/')
      respond(if (id == "sync-index") index(listOf(recordId, missing)) else detail(id),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }
    repository(engine).use { it.synchronize(token, accountId) }
    repository(MockEngine { request ->
      if (request.url.parameters["cursor"] != null) throw IOException("synthetic_failure")
      respond(index(cursor = "next.signature"), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      try { records.synchronize(token, accountId); fail("partial enumeration cannot prune") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.UNAVAILABLE, error.failure) }
      assertEquals(2, records.cachedRecords(accountId).size)
      assertEquals(setOf(missing), records.syncCheckpoint(accountId)?.removalCandidates)
    }
    repository(MockEngine { request ->
      if (request.url.encodedPath.endsWith("/sync-index")) {
        respond(index(emptyList()), headers = headersOf(HttpHeaders.ContentType, "application/json"))
      } else respond("", HttpStatusCode.NotFound)
    })
      .use { records ->
        records.synchronize(token, accountId) // Resume the final page, not a fresh enumeration.
        assertEquals(listOf(recordId), records.cachedRecords(accountId).map { it.recordId })
        assertNull(records.cachedRecord(accountId, missing))
        assertEquals("架空の結論", records.cachedRecord(accountId, recordId)?.decision)
      }
  }

  @Test fun indexOmissionKeepsLiveRecordsAndResumesFailedDeletionChecks() = runBlocking {
    repository(MockEngine { request ->
      respond(if (request.url.encodedPath.endsWith("/sync-index")) index() else detail(),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { it.synchronize(token, accountId) }
    repository(MockEngine { request ->
      if (request.url.encodedPath.endsWith("/sync-index")) {
        respond(index(emptyList()), headers = headersOf(HttpHeaders.ContentType, "application/json"))
      } else respond("", HttpStatusCode.ServiceUnavailable)
    }).use { records ->
      try { records.synchronize(token, accountId); fail("failed confirmation must preserve local data") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.UNAVAILABLE, error.failure) }
      assertEquals(setOf(recordId), records.syncCheckpoint(accountId)?.removalCandidates)
      assertEquals("架空の結論", records.cachedRecord(accountId, recordId)?.decision)
    }
    val calls = mutableListOf<String>()
    repository(MockEngine { request ->
      calls.add(request.url.encodedPath.substringAfterLast('/'))
      // Head refresh may find no result, but existence confirmation must not replace a live cache.
      respond(if (request.url.encodedPath.endsWith("/sync-index")) index(emptyList())
        else detail().replace("架空の結論", "別の結論"),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { records ->
      records.synchronize(token, accountId)
      assertEquals(listOf("sync-index", recordId), calls)
      assertEquals(listOf(recordId), records.cachedRecords(accountId).map { it.recordId })
      assertEquals("架空の結論", records.cachedRecord(accountId, recordId)?.decision)
      assertEquals(true, records.syncCheckpoint(accountId)?.complete)
    }
  }

  @Test fun confirmed404RemovesListAndDetailButServerFailureDoesNot() = runBlocking {
    repository(MockEngine { request ->
      respond(if (request.url.encodedPath.endsWith("/$recordId")) detail() else index(),
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { it.synchronize(token, accountId) }
    for (status in listOf(HttpStatusCode.ServiceUnavailable, HttpStatusCode.NotFound)) {
      repository(MockEngine { respond("", status) }).use { records ->
        try { records.record(token, accountId, recordId); fail("HTTP failure must be reported") }
        catch (error: RecordReadException) {
          assertEquals(if (status == HttpStatusCode.NotFound) RecordReadFailure.NOT_FOUND
            else RecordReadFailure.UNAVAILABLE, error.failure)
        }
        assertEquals(if (status == HttpStatusCode.NotFound) 0 else 1, records.cachedRecords(accountId).size)
        assertEquals(status != HttpStatusCode.NotFound, records.cachedRecord(accountId, recordId) != null)
      }
    }
  }

  @Test fun failedDatabaseUpdatePreservesThePreviousEncryptedDetail() = runBlocking {
    repository(MockEngine { respond(detail(), headers = headersOf(HttpHeaders.ContentType, "application/json")) })
      .use { it.record(token, accountId, recordId) }
    AndroidSQLiteDriver().open(app.getDatabasePath(databaseName).path).use { connection ->
      // Simulate a rejected write (e.g. exhausted storage), not a destructive database corruption.
      connection.prepare("CREATE TRIGGER reject_record_write BEFORE INSERT ON encrypted_records BEGIN SELECT RAISE(ABORT, 'synthetic_write_failure'); END")
        .use { it.step() }
    }
    repository(MockEngine { respond(detail().replace("架空の結論", "更新後の結論"),
      headers = headersOf(HttpHeaders.ContentType, "application/json")) }).use { records ->
      try { records.record(token, accountId, recordId); fail("save failure must be reported") }
      catch (error: RecordReadException) { assertEquals(RecordReadFailure.STORAGE_UNAVAILABLE, error.failure) }
      assertEquals("架空の結論", records.cachedRecord(accountId, recordId)?.decision)
    }
  }

  @Test fun authorizationExpiryLocksButSameAccountReauthenticationKeepsTheCiphertext() = runBlocking {
    repository(MockEngine {
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { it.recentRecords(token, accountId, null) }
    activeAccountId = "" // The session gate is closed, but no deletion is requested.
    repository(MockEngine { error("unexpected_network_request") }).use { cached ->
      try {
        cached.cachedListEntry(accountId, recordId)
        fail("expired authorization must lock the cache")
      } catch (error: RecordReadException) {
        assertEquals(RecordReadFailure.AUTH_REQUIRED, error.failure)
      }
      activeAccountId = accountId
      assertEquals("架空の議題", cached.cachedListEntry(accountId, recordId)?.questionPreview)
    }
  }

  @Test fun explicitLogoutInvalidatesPrivateKeyAndErasesAllRowsAndOwnerMarker() = runBlocking {
    repository(MockEngine {
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use {
      it.recentRecords(token, accountId, null)
      it.saveSyncCheckpoint(accountId, RecordSyncCheckpoint())
    }
    val unrelated = File(privateDirectory, "unrelated").apply { writeText("keep") }
    val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    try {
      RecordCacheAccount.lock.withLock {
        RecordCacheAccount(privateContext, privateKeys, database).clear()
      }
      assertNull(privateKeys.read(accountId))
      assertNull(EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys))
        .load(accountId, recordId, CachedRecordPart.LIST))
      assertEquals(listOf(unrelated.name), privateDirectory.listFiles()!!.map { it.name })
    } finally { database.close() }
    // The same account can start a genuinely empty cache after an explicit logout.
    repository(MockEngine {
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { assertEquals(1, it.recentRecords(token, accountId, null).items.size) }
  }

  @Test fun interruptedLogoutResumesDeletionBeforeTheSameAccountCanReadAgain() = runBlocking {
    repository(MockEngine {
      respond(list(), headers = headersOf(HttpHeaders.ContentType, "application/json"))
    }).use { it.recentRecords(token, accountId, null) }
    val closed = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    closed.records().get("unused", recordId, "list") // Open, then force a real deletion failure.
    closed.close()
    RecordCacheAccount.lock.withLock {
      try {
        RecordCacheAccount(privateContext, privateKeys, closed).clear()
        fail("closed database must not report successful logout cleanup")
      } catch (_: RecordCacheException) { }
    }
    assertNull(privateKeys.read(accountId))
    assertEquals(true, File(privateDirectory, "records-cache-delete.v1").exists())
    val reopened = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    try {
      RecordCacheAccount.lock.withLock {
        RecordCacheAccount(privateContext, privateKeys, reopened).activate(accountId)
      }
      assertNull(EncryptedRecordStore(reopened, RecordDataKeyProtector(privateKeys))
        .load(accountId, recordId, CachedRecordPart.LIST))
      assertFalse(File(privateDirectory, "records-cache-delete.v1").exists())
    } finally { reopened.close() }
  }

  private fun repository(engine: MockEngine): RecordsRepository {
    val database = Room.databaseBuilder<EncryptedRecordsDatabase>(app, databaseName)
      .setDriver(AndroidSQLiteDriver()).build()
    return RecordsRepository(RecordsReadClient(engine),
      EncryptedRecordStore(database, RecordDataKeyProtector(privateKeys)), database,
      RecordCacheAccount(privateContext, privateKeys, database), { it == activeAccountId })
  }

  private fun index(ids: List<String> = listOf(recordId), cursor: String? = null): String =
    """{"schemaVersion":1,"items":[${ids.joinToString { """{"recordId":"$it","revision":"${"z".repeat(43)}","avatarRevision":"${"i".repeat(43)}"}""" }}],"nextCursor":${cursor?.let { "\"$it\"" } ?: "null"}}"""

  private fun list(ids: List<String> = listOf(recordId), cursor: String? = null): String =
    """{"schemaVersion":1,"items":[${ids.joinToString { listItem(it) }}],"nextCursor":${cursor?.let { "\"$it\"" } ?: "null"}}"""

  private fun listItem(id: String): String = """{"schemaVersion":1,"recordId":"$id",
    "questionPreview":"架空の議題","completedAt":"2026-09-24T00:00:00Z",
    "requester":{"displayName":"架空の依頼者","avatar":{"kind":"placeholder",
      "url":null,"alt":"架空の依頼者","fallbackVariant":"cyan"}},
    "participants":[{"slot":"participant-a","displayName":"アロナ"},
      {"slot":"participant-b","displayName":"プラナ"},
      {"slot":"participant-c","displayName":"安倍晋三AI"}],
    "result":{"winner":"participant-a"}}"""

  private fun detail(id: String = recordId): String = """{"schemaVersion":2,"recordId":"$id","question":"架空の議題",
    "completedAt":"2026-09-24T00:00:00Z",
    "requester":{"displayName":"架空の依頼者","avatar":{"kind":"placeholder","url":null,"alt":"依頼者","fallbackVariant":"cyan"}},
    "participants":[{"slot":"participant-a","displayName":"アロナ"},
      {"slot":"participant-b","displayName":"プラナ"},
      {"slot":"participant-c","displayName":"安倍晋三AI"}],
    "initialOpinions":[{"participant":"participant-a","summary":"要約A","proposal":"案A"},
      {"participant":"participant-b","summary":"要約B","proposal":"案B"},
      {"participant":"participant-c","summary":"要約C","proposal":"案C"}],
    "finalProposals":[{"participant":"participant-a","title":"最終案A","proposal":"決定案A"},
      {"participant":"participant-b","title":"最終案B","proposal":"決定案B"},
      {"participant":"participant-c","title":"最終案C","proposal":"決定案C"}],
    "votes":[{"voter":"participant-a","candidate":"participant-b","reason":"理由A"},
      {"voter":"participant-b","candidate":"participant-a","reason":"理由B"},
      {"voter":"participant-c","candidate":"participant-a","reason":"理由C"}],
    "result":{"winner":"participant-a","voteCounts":[
      {"participant":"participant-a","count":2},{"participant":"participant-b","count":1},
      {"participant":"participant-c","count":0}],"tieBreakApplied":false},
    "finalDecision":{"winner":"participant-a","decision":"架空の結論","actions":[],"caveats":[]},
    "affection":null}"""

  private fun detailWithReasons(id: String, legacy: Boolean = false): String {
    val reasons = if (legacy) listOf("", "", "") else listOf(
      ",\"reason\":\"架空の率直な感想\",\"reasonStatus\":\"available\"",
      ",\"reason\":null,\"reasonStatus\":\"unavailable\"",
      ",\"reason\":null,\"reasonStatus\":\"not_recorded\"")
    return detail(id).replace("\"affection\":null", """"affection":{"status":"applied",
      "rubricVersion":"v1","participants":[
      {"participant":"participant-a","before":500,"questionScore":10,"appliedDelta":10,"after":510${reasons[0]}},
      {"participant":"participant-b","before":500,"questionScore":-20,"appliedDelta":-20,"after":480${reasons[1]}},
      {"participant":"participant-c","before":500,"questionScore":0,"appliedDelta":0,"after":500${reasons[2]}}]}""")
  }
}
