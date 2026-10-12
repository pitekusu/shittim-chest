package dev.pitekusu.shittim.records

import java.io.Closeable

/** The same reader UI consumes authorized saved records or an isolated local demo. */
internal interface RecordsReader : Closeable {
  suspend fun cachedRecords(accountId: String): List<RecordListEntry>
  suspend fun cachedRecord(accountId: String, recordId: String): RecordPreview?
  suspend fun queryCachedRecords(accountId: String, entries: List<RecordListEntry>,
    query: RecordListQuery): List<RecordListEntry>
  suspend fun cachedAttachment(accountId: String, recordId: String, image: RecordAttachment): ByteArray?
}
