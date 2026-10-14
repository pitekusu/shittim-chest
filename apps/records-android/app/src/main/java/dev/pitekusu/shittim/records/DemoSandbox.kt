package dev.pitekusu.shittim.records

import android.content.Context
import android.net.Uri
import dev.pitekusu.shittim.records.storage.CachedRecordPart
import dev.pitekusu.shittim.records.storage.EncryptedRecordImageStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordStore
import dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase
import dev.pitekusu.shittim.records.storage.KeystorePrivateKeyStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.RecordDataKeyProtector
import dev.pitekusu.shittim.records.storage.StorageNamespace
import java.security.MessageDigest
import java.time.Duration
import java.time.Instant
import java.util.Base64
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

/** Local-only review backend. It never creates authentication, HTTP, AI, Discord, or Firebase clients. */
internal class DemoSandbox(
  context: Context,
  private val entryId: String,
  private val onPublished: (String) -> Unit = {},
  private val clock: () -> Instant = Instant::now,
) : RecordsReader {
  private val app = context.applicationContext
  private val mode = ReviewDemoModeStore(app)
  init { check(validDebateRequestId(entryId) && mode.isDemo(entryId)) }
  private val database = EncryptedRecordsDatabase.open(app, StorageNamespace.DEMO)
  private val keys = KeystorePrivateKeyStore(app, StorageNamespace.DEMO)
  private val cache = EncryptedRecordStore(database, RecordDataKeyProtector(keys))
  private val owner = RecordCacheAccount(app, keys, database, StorageNamespace.DEMO)
  private val images = EncryptedRecordImageStore(app, namespace = StorageNamespace.DEMO)
  private val workspace = lazy {
    DebateWorkspaceStore.open(app, DEMO_ACCOUNT_ID,
      { account -> account == DEMO_ACCOUNT_ID && authorized() }, StorageNamespace.DEMO)
  }
  private val json = Json { encodeDefaults = true }
  private val mutableChanges = MutableStateFlow(0L)
  val changes = mutableChanges.asStateFlow()
  @Volatile private var closed = false

  private fun authorized(): Boolean = !closed && mode.isDemo(entryId)
  fun isActive(): Boolean = authorized()

  private suspend fun guard(accountId: String = DEMO_ACCOUNT_ID) {
    if (accountId != DEMO_ACCOUNT_ID || !authorized()) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
    owner.requireOwner(DEMO_ACCOUNT_ID)
    if (!authorized()) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
  }

  suspend fun load(): DebateWorkspace = debateOperation {
    if (!authorized()) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
    workspace.value.load(DEMO_ACCOUNT_ID)
  }

  suspend fun save(value: DebateWorkspace): Unit = debateOperation {
    if (!authorized()) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
    workspace.value.save(DEMO_ACCOUNT_ID, value)
  }

  suspend fun importImage(uri: Uri): DraftAttachment = debateOperation {
    if (!authorized()) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
    if (uri == DEMO_SAMPLE_IMAGE_URI) {
      val bytes = DemoFixtures.sampleImageBytes()
      try { workspace.value.importBytes(DEMO_ACCOUNT_ID, bytes) }
      finally { bytes.fill(0) }
    } else workspace.value.importImage(DEMO_ACCOUNT_ID, uri)
  }

  suspend fun readImage(image: DraftAttachment): ByteArray = debateOperation {
    if (!authorized()) throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
    workspace.value.image(DEMO_ACCOUNT_ID, image)
  }

  suspend fun submit(requestId: String, question: String,
    attachments: List<DraftAttachment> = emptyList()): DebateRequest = debateOperation {
    if (!validDebateRequestId(requestId) || !validDebateQuestion(question))
      throw DebateRequestException(DebateFailure.INVALID_RESPONSE)
    if (attachments.size > MAX_ATTACHMENT_COUNT || attachments.map { it.attachmentId }.distinct().size != attachments.size)
      throw DebateRequestException(DebateFailure.ATTACHMENT_INVALID)
    try { attachments.forEach(DraftAttachment::validate) }
    catch (_: Exception) { throw DebateRequestException(DebateFailure.ATTACHMENT_INVALID) }
    RecordCacheAccount.lock.withLock {
      guard()
      loadRequest(requestId)?.let { existing ->
        checkSameSubmission(existing, question, attachments)
        return@debateOperation advance(existing).request
      }
    }
    // Copy originals into the completed record's own binding before accepting the request.
    // Starting a new draft may prune its images without affecting this sandbox result.
    val originals = mutableListOf<Pair<DraftAttachment, ByteArray>>()
    try {
      attachments.forEach { originals += it to readImage(it) }
      RecordCacheAccount.lock.withLock {
        guard()
        loadRequest(requestId)?.let { existing ->
          checkSameSubmission(existing, question, attachments)
          return@withLock advance(existing).request
        }
        val recordId = demoResultId(requestId)
        originals.forEach { (image, bytes) ->
          images.save(DEMO_ACCOUNT_ID, recordId, image.descriptor(), bytes)
          guard()
        }
        val now = clock().toString()
        val request = DebateRequest(requestId, question, "accepted", createdAt = now, updatedAt = now,
          attachmentIds = attachments.map { it.attachmentId })
        val snapshot = DemoRequestSnapshot(1, entryId, request, recordId, attachments.map { it.descriptor() })
        storeRequest(snapshot)
        guard()
        mutableChanges.value++
        request
      }
    } finally { originals.forEach { it.second.fill(0) } }
  }

  suspend fun find(requestId: String): DebateRequest? = debateOperation {
    if (!validDebateRequestId(requestId)) throw DebateRequestException(DebateFailure.INVALID_RESPONSE)
    RecordCacheAccount.lock.withLock { guard(); loadRequest(requestId)?.let { advance(it) }?.request }
  }

  suspend fun list(cursor: String? = null): DebateRequestPage = debateOperation {
    val offset = if (cursor == null) 0 else {
      if (!cursor.matches(Regex("demo:[0-9]{1,6}"))) throw DebateRequestException(DebateFailure.INVALID_RESPONSE)
      cursor.removePrefix("demo:").toInt()
    }
    RecordCacheAccount.lock.withLock {
      guard()
      val requests = loadRequests().map { advance(it).request }
        .sortedWith(compareByDescending<DebateRequest> { Instant.parse(it.createdAt) }.thenBy { it.requestId })
      val end = minOf(requests.size, offset + 50)
      DebateRequestPage(requests.drop(offset).take(50), "demo:$end".takeIf { end < requests.size })
    }
  }

  override suspend fun cachedRecords(accountId: String): List<RecordListEntry> = recordOperation {
    RecordCacheAccount.lock.withLock {
      guard(accountId)
      (DemoFixtures.records.map { it.entry } + loadRequests().map { advance(it) }
        .filter { it.request.status == "published" }.map { generated(it).entry })
        .sortedWith(compareByDescending<RecordListEntry> { it.completedAt }.thenBy { it.recordId })
        .also { guard(accountId) }
    }
  }

  override suspend fun cachedRecord(accountId: String, recordId: String): RecordPreview? = recordOperation {
    RecordCacheAccount.lock.withLock {
      guard(accountId)
      (DemoFixtures.records.firstOrNull { it.entry.recordId == recordId }?.preview
        ?: loadRequests().firstOrNull { it.recordId == recordId }?.let { advance(it) }
          ?.takeIf { it.request.status == "published" }?.let { generated(it).preview })
        .also { guard(accountId) }
    }
  }

  override suspend fun queryCachedRecords(accountId: String, entries: List<RecordListEntry>,
    query: RecordListQuery): List<RecordListEntry> = recordOperation {
    withContext(Dispatchers.Default) {
      guard(accountId)
      val selected = entries.filter { entry ->
        guard(accountId)
        query.acceptsWinner(entry) && query.acceptsRequester(entry) &&
          (!query.searchesText || query.matches(entry) || query.matches(entry, cachedRecord(accountId, entry.recordId)))
      }
      guard(accountId)
      query.sorted(selected)
    }
  }

  override suspend fun cachedAttachment(accountId: String, recordId: String,
    image: RecordAttachment): ByteArray? = recordOperation {
    RecordCacheAccount.lock.withLock {
      guard(accountId)
      val fixture = DemoFixtures.records.firstOrNull { it.entry.recordId == recordId }
      val bytes = if (fixture != null) {
        if (fixture.preview.attachments.none { it.sameImage(image) }) null else DemoFixtures.sampleImageBytes(image)
      } else {
        val snapshot = loadRequests().firstOrNull { it.recordId == recordId }?.let { advance(it) }
        if (snapshot?.request?.status != "published" || snapshot.images.none { it.sameImage(image) }) null
        else images.read(DEMO_ACCOUNT_ID, recordId, image)
      }
      try { guard(accountId); bytes }
      catch (error: Exception) { bytes?.fill(0); throw error }
    }
  }

  /** Clock rollback rebases the remaining short simulation rather than waiting for a future date. */
  private suspend fun advance(snapshot: DemoRequestSnapshot): DemoRequestSnapshot {
    guard()
    val request = snapshot.request
    val current = STAGES.indexOf(request.status).also { check(it >= 0) }
    val now = clock()
    val progress = if (now < Instant.parse(snapshot.progressAnchorAt)) {
      DemoRequestSnapshot(1, entryId, request, snapshot.recordId, snapshot.images,
        now.toString(), current * 2L).also { storeRequest(it) }
    } else snapshot
    val elapsed = progress.elapsedBaseSeconds + Duration.between(Instant.parse(progress.progressAnchorAt), now).seconds
    val next = maxOf(current, minOf(STAGES.lastIndex.toLong(), elapsed / 2).toInt())
    if (current == next) return progress
    val status = STAGES[next]
    val updatedAt = Instant.parse(request.createdAt).plusSeconds(next * 2L).toString()
    val receipt = DebateRequest(request.requestId, request.question, status,
      phase = when (status) { "starting" -> "preparing_evidence"; "running" -> "discussing"; "publishing" -> "generating_decision"; else -> null },
      createdAt = request.createdAt, updatedAt = updatedAt,
      recordId = snapshot.recordId.takeIf { status == "published" }, attachmentIds = request.attachmentIds)
    val advanced = DemoRequestSnapshot(1, entryId, receipt, snapshot.recordId, snapshot.images,
      progress.progressAnchorAt, progress.elapsedBaseSeconds)
    storeRequest(advanced)
    guard()
    if (status == "published") {
      mutableChanges.value++
      // Completion must remain durable even if displaying a local notification fails.
      try { onPublished(snapshot.recordId) } catch (_: Exception) { }
    }
    return advanced
  }

  private fun generated(snapshot: DemoRequestSnapshot): DemoRecord {
    val question = snapshot.request.question
    val summary = question.replace(Regex("[\\s\\p{Z}]+"), " ").trim().let { text ->
      text.substring(0, text.offsetByCodePoints(0, minOf(160, text.codePointCount(0, text.length))))
    }
    return DemoRecord(RecordListEntry(snapshot.recordId, summary, "デモ利用者", RecordAvatar(null, "default"),
      Instant.parse(snapshot.request.updatedAt), "アロナ", "participant-a", snapshot.images.size),
      DemoFixtures.result(question, snapshot.images))
  }

  private fun checkSameSubmission(snapshot: DemoRequestSnapshot, question: String, attachments: List<DraftAttachment>) {
    if (snapshot.request.question != question || snapshot.images.size != attachments.size ||
      !snapshot.images.zip(attachments).all { (stored, draft) -> stored.sameImage(draft.descriptor()) })
      throw DebateRequestException(DebateFailure.CONFLICT)
  }

  private suspend fun loadRequest(requestId: String): DemoRequestSnapshot? {
    val bytes = cache.load(DEMO_ACCOUNT_ID, requestId, CachedRecordPart.DEMO_SANDBOX) ?: return null
    return decode(bytes, requestId)
  }

  private suspend fun loadRequests(): List<DemoRequestSnapshot> =
    cache.rows(DEMO_ACCOUNT_ID, CachedRecordPart.DEMO_SANDBOX).map { row ->
      guard()
      decode(cache.decryptRow(DEMO_ACCOUNT_ID, row, CachedRecordPart.DEMO_SANDBOX), row.recordId)
    }

  private fun decode(bytes: ByteArray, requestId: String): DemoRequestSnapshot = try {
    json.decodeFromString<DemoRequestSnapshot>(bytes.decodeToString(throwOnInvalidSequence = true)).also {
      validate(it)
      check(it.request.requestId == requestId)
    }
  } finally { bytes.fill(0) }

  private suspend fun storeRequest(snapshot: DemoRequestSnapshot) {
    guard()
    validate(snapshot)
    val bytes = json.encodeToString(snapshot).encodeToByteArray()
    try { cache.save(DEMO_ACCOUNT_ID, snapshot.request.requestId, CachedRecordPart.DEMO_SANDBOX, bytes) }
    finally { bytes.fill(0) }
    guard()
  }

  private fun validate(snapshot: DemoRequestSnapshot) {
    check(snapshot.schemaVersion == 1 && snapshot.entryId == entryId && snapshot.recordId == demoResultId(snapshot.request.requestId))
    snapshot.request.validate()
    check(snapshot.request.status in STAGES && snapshot.images.map { it.attachmentId } == snapshot.request.attachmentIds)
    snapshot.images.forEach { it.validate(); check(it.url == null && it.urlExpiresAt == null) }
    check(snapshot.request.recordId == snapshot.recordId.takeIf { snapshot.request.status == "published" })
    check(snapshot.progressAnchorAt.endsWith("Z"))
    Instant.parse(snapshot.progressAnchorAt)
    check(snapshot.elapsedBaseSeconds in 0..(STAGES.indexOf(snapshot.request.status) * 2L))
  }

  private suspend fun <T> debateOperation(action: suspend () -> T): T = try { action() }
  catch (error: CancellationException) { throw error }
  catch (error: DebateRequestException) { throw error }
  catch (error: RecordImageException) { throw error }
  catch (_: Exception) { throw DebateRequestException(DebateFailure.STORAGE) }

  private suspend fun <T> recordOperation(action: suspend () -> T): T = try { action() }
  catch (error: CancellationException) { throw error }
  catch (error: RecordReadException) { throw error }
  catch (error: RecordImageException) { throw error }
  catch (error: DebateRequestException) {
    throw RecordReadException(if (error.failure == DebateFailure.AUTH_REQUIRED) RecordReadFailure.AUTH_REQUIRED
      else RecordReadFailure.STORAGE_UNAVAILABLE)
  }
  catch (_: Exception) { throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }

  override fun close() {
    if (closed) return
    closed = true
    try { if (workspace.isInitialized()) workspace.value.close() } finally { database.close() }
  }

  private companion object {
    val STAGES = listOf("accepted", "queued", "starting", "running", "publishing", "published")
  }
}

private fun demoResultId(requestId: String): String = Base64.getUrlEncoder().withoutPadding().encodeToString(
  MessageDigest.getInstance("SHA-256").digest("review-demo-result|$requestId".toByteArray(Charsets.US_ASCII)))

@Serializable
private class DemoRequestSnapshot(val schemaVersion: Int, val entryId: String, val request: DebateRequest,
  val recordId: String, val images: List<RecordAttachment>,
  val progressAnchorAt: String = request.createdAt, val elapsedBaseSeconds: Long = 0)
