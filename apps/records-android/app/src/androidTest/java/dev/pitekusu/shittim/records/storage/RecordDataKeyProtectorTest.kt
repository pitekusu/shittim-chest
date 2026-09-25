package dev.pitekusu.shittim.records.storage

import android.content.Context
import android.content.ContextWrapper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.nio.file.Files
import java.util.UUID
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordDataKeyProtectorTest {
  private lateinit var context: Context
  private lateinit var directory: File
  private lateinit var privateKeys: KeystorePrivateKeyStore
  private lateinit var protector: RecordDataKeyProtector
  private val account = "synthetic_account"
  private val record = "synthetic_record"
  private val dataKey = ByteArray(32) { it.toByte() }

  @Before
  fun setUp() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "record-key-test-").toFile()
    val testPackage = "${app.packageName}.test.${UUID.randomUUID()}"
    context = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = testPackage
    }
    privateKeys = KeystorePrivateKeyStore(context)
    protector = RecordDataKeyProtector(privateKeys)
  }

  @After
  fun tearDown() {
    privateKeys.clear()
    directory.deleteRecursively()
  }

  @Test
  fun wrappedKeySurvivesNewInstanceAndUsesFreshEncapsulation() {
    val first = protector.wrap(account, record, dataKey)
    val firstPrivate = privateKeys.read(account)!!
    val second = protector.wrap(account, record, dataKey)
    val secondPrivate = privateKeys.read(account)!!
    assertArrayEquals(firstPrivate, secondPrivate)
    firstPrivate.fill(0)
    secondPrivate.fill(0)
    assertEquals(1.toByte(), first[0])
    assertEquals(1 + 1088 + 48, first.size)
    assertFalse(first.contentEquals(second))
    val reopened = RecordDataKeyProtector(KeystorePrivateKeyStore(context))
    assertArrayEquals(dataKey, reopened.unwrap(account, record, first))
    assertArrayEquals(dataKey, reopened.unwrap(account, record, second))
  }

  @Test
  fun alteredEnvelopeOrDifferentRecordAndAccountCannotUnwrap() {
    val wrapped = protector.wrap(account, record, dataKey)
    assertProtectionFailure { protector.unwrap(account, "another_record", wrapped) }
    assertProtectionFailure { protector.unwrap("another_account", record, wrapped) }
    assertProtectionFailure { protector.wrap("another_account", record, dataKey) }
    for (altered in listOf(
      wrapped.copyOf().apply { this[0] = 2 },
      wrapped.copyOf().apply { this[20] = (this[20].toInt() xor 1).toByte() },
      wrapped.copyOf().apply { this[lastIndex] = (this[lastIndex].toInt() xor 1).toByte() },
      wrapped.copyOf(wrapped.size - 1),
    )) {
      assertProtectionFailure { protector.unwrap(account, record, altered) }
    }
    assertArrayEquals(dataKey, protector.unwrap(account, record, wrapped))
  }

  @Test
  fun missingOrInvalidPrivateKeyNeverCreatesAReplacement() {
    val wrapped = protector.wrap(account, record, dataKey)
    privateKeys.clear()
    assertProtectionFailure { protector.unwrap(account, record, wrapped) }
    assertNull(privateKeys.read(account))
    privateKeys.save(account, byteArrayOf(2, 3))
    assertProtectionFailure { protector.unwrap(account, record, wrapped) }
    assertProtectionFailure { protector.wrap(account, record, dataKey) }
  }

  @Test
  fun invalidInputsDoNotCreateStoredKey() {
    assertProtectionFailure { protector.wrap(account, record, ByteArray(31)) }
    assertProtectionFailure { protector.wrap(account, "invalid/record", dataKey) }
    assertProtectionFailure { protector.wrap("invalid/account", record, dataKey) }
    assertNull(privateKeys.read(account))
  }

  private fun assertProtectionFailure(operation: () -> Any?) {
    val error = assertThrows(RecordKeyProtectionException::class.java) { operation() }
    assertEquals("record_key_protection_failed", error.message)
    assertNull(error.cause)
  }
}
