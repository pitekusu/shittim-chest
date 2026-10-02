package dev.pitekusu.shittim.records

import androidx.paging.PagingData
import androidx.paging.insertSeparators
import androidx.paging.map
import java.time.LocalDate
import java.time.ZoneId
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

/** Presentation rows only: dates are never persisted alongside encrypted records. */
internal sealed interface RecordJournalRow {
  val stableKey: String

  data class DateHeading(val date: LocalDate) : RecordJournalRow {
    override val stableKey: String = "journal-date:$date"
  }

  data class Record(val entry: RecordListEntry) : RecordJournalRow {
    // Preserve the existing card key so insertion of a date or record does not lose its anchor.
    override val stableKey: String = entry.recordId
  }
}

internal val recordJournalZone: ZoneId = ZoneId.of("Asia/Tokyo")

internal fun recordJournalDate(entry: RecordListEntry): LocalDate =
  entry.completedAt.atZone(recordJournalZone).toLocalDate()

/** Paging owns page-boundary separators, including the first loaded day in either sort order. */
internal fun PagingData<RecordListEntry>.asRecordJournal(): PagingData<RecordJournalRow> =
  map { RecordJournalRow.Record(it) }.insertSeparators { before, after ->
    after?.let { row ->
      val day = recordJournalDate(row.entry)
      if (before == null || recordJournalDate(before.entry) != day) RecordJournalRow.DateHeading(day)
      else null
    }
  }

internal fun Flow<PagingData<RecordListEntry>>.asRecordJournal(): Flow<PagingData<RecordJournalRow>> =
  map { it.asRecordJournal() }
