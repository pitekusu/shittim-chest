package dev.pitekusu.shittim.records.storage

import android.content.Context
import androidx.room3.ColumnInfo
import androidx.room3.Dao
import androidx.room3.Database
import androidx.room3.Entity
import androidx.room3.Query
import androidx.room3.Room
import androidx.room3.RoomDatabase
import androidx.room3.Upsert
import androidx.sqlite.driver.AndroidSQLiteDriver

/** Only opaque identifiers and authenticated ciphertext belong in this table. */
@Entity(tableName = "encrypted_records", primaryKeys = ["account_key", "record_id", "part"])
internal class EncryptedRecordRow(
  @ColumnInfo(name = "account_key") val accountKey: String,
  @ColumnInfo(name = "record_id") val recordId: String,
  val part: String,
  @ColumnInfo(name = "wrapped_key") val wrappedKey: ByteArray,
  @ColumnInfo(name = "encrypted_payload") val encryptedPayload: ByteArray,
)

@Dao
internal interface EncryptedRecordDao {
  @Upsert
  suspend fun put(row: EncryptedRecordRow)

  @Query("SELECT * FROM encrypted_records WHERE account_key = :accountKey AND record_id = :recordId AND part = :part")
  suspend fun get(accountKey: String, recordId: String, part: String): EncryptedRecordRow?

  @Query("SELECT record_id FROM encrypted_records WHERE account_key = :accountKey AND part = :part")
  suspend fun recordIds(accountKey: String, part: String): List<String>

  @Query("DELETE FROM encrypted_records WHERE account_key = :accountKey AND record_id IN (:recordIds)")
  suspend fun deleteRecords(accountKey: String, recordIds: List<String>)

  @Query("DELETE FROM encrypted_records WHERE account_key = :accountKey")
  suspend fun deleteAccount(accountKey: String)

  @Query("DELETE FROM encrypted_records")
  suspend fun deleteAll()
}

@Database(entities = [EncryptedRecordRow::class], version = 1, exportSchema = true)
internal abstract class EncryptedRecordsDatabase : RoomDatabase() {
  abstract fun records(): EncryptedRecordDao

  companion object {
    fun open(context: Context): EncryptedRecordsDatabase =
      Room.databaseBuilder<EncryptedRecordsDatabase>(context.applicationContext, "encrypted-records.db")
        .setDriver(AndroidSQLiteDriver())
        .build()
  }
}
