package dev.pitekusu.shittim.records.storage

import android.content.Context
import android.content.ContextWrapper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.nio.file.Files
import java.security.KeyStore
import java.util.UUID
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class KeystorePrivateKeyStoreTest {
  private lateinit var context: Context
  private lateinit var directory: File
  private lateinit var alias: String
  private lateinit var store: KeystorePrivateKeyStore
  private val owner = "synthetic_account"
  private val privateKey = ByteArray(2_400) { it.toByte() }
  private val file get() = File(directory, "records-private-key.v1")

  @Before
  fun setUp() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "private-key-test-").toFile()
    val testPackage = "${app.packageName}.test.${UUID.randomUUID()}"
    context = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = testPackage
    }
    alias = "$testPackage.records-private-key.v1"
    store = KeystorePrivateKeyStore(context)
  }

  @After
  fun tearDown() {
    store.clear()
    directory.deleteRecursively()
  }

  @Test
  fun versionedEnvelopeSurvivesNewInstanceWithoutExportingKeystoreKey() {
    assertNull(store.read(owner))
    store.save(owner, privateKey)
    val first = file.readBytes()
    assertEquals(1.toByte(), first[0])
    assertFalse(first.toString(Charsets.ISO_8859_1).contains(privateKey.toString(Charsets.ISO_8859_1)))
    assertNull(keys().getKey(alias, null).encoded)
    assertArrayEquals(privateKey, KeystorePrivateKeyStore(context).read(owner))
    store.save(owner, privateKey)
    assertFalse(first.contentEquals(file.readBytes())) // Fresh GCM IV per write.
  }

  @Test
  fun wrongAccountAndTamperedOrOversizedEnvelopeDoNotOverwriteTheKey() {
    store.save(owner, privateKey)
    val original = file.readBytes()
    assertStorageFailure { store.read("another_account") }
    assertStorageFailure { store.save("another_account", privateKey) }
    assertStorageFailure { store.save(owner, ByteArray(privateKey.size) { 7 }) }
    assertArrayEquals(original, file.readBytes())
    for (invalid in listOf(
      original.copyOf().apply { this[lastIndex] = (this[lastIndex].toInt() xor 1).toByte() },
      original.copyOf().apply { this[0] = 2 },
      original.copyOf(original.size - 1),
      ByteArray(16 * 1024 + 31),
    )) {
      file.writeBytes(invalid)
      assertStorageFailure { store.read(owner) }
      assertStorageFailure { store.save(owner, privateKey) }
      assertArrayEquals(invalid, file.readBytes())
    }
  }

  @Test
  fun lostKeystoreKeyRequiresExplicitClearAndClearPreservesOtherFiles() {
    val other = File(directory, "unrelated").apply { writeText("keep") }
    store.save(owner, privateKey)
    keys().deleteEntry(alias)
    assertStorageFailure { store.read(owner) }
    assertStorageFailure { store.save(owner, privateKey) }
    store.clear()
    assertNull(store.read(owner))
    assertEquals("keep", other.readText())
    store.save(owner, privateKey)
    assertArrayEquals(privateKey, store.read(owner))
  }

  @Test
  fun invalidInputsFailWithoutCreatingAKeyOrFile() {
    assertStorageFailure { store.save("invalid/account", privateKey) }
    assertStorageFailure { store.save(owner, ByteArray(0)) }
    assertStorageFailure { store.save(owner, ByteArray(16 * 1024 + 1)) }
    assertFalse(file.exists())
    assertFalse(keys().containsAlias(alias))
    assertTrue(directory.canonicalPath.startsWith(
      InstrumentationRegistry.getInstrumentation().targetContext.noBackupFilesDir.canonicalPath + "/"))
  }

  @Test
  fun failedFirstWriteReleasesItsNewKeyForSafeRetry() {
    val blocked = File(directory, "blocked-parent").apply { writeText("keep") }
    val blockedContext = object : ContextWrapper(context) {
      override fun getNoBackupFilesDir(): File = blocked
    }
    assertStorageFailure { KeystorePrivateKeyStore(blockedContext).save(owner, privateKey) }
    assertFalse(keys().containsAlias(alias))
    assertEquals("keep", blocked.readText())
    store.save(owner, privateKey)
    assertArrayEquals(privateKey, store.read(owner))
  }

  private fun assertStorageFailure(operation: () -> Any?) {
    val error = assertThrows(PrivateKeyStorageException::class.java) { operation() }
    assertEquals("private_key_storage_unavailable", error.message)
    assertNull(error.cause)
  }

  private fun keys(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
}
