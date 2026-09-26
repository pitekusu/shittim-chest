package dev.pitekusu.shittim.records.auth

import android.content.Context
import android.content.ContextWrapper
import android.content.pm.ApplicationInfo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.nio.file.Files
import java.security.KeyStore
import java.time.Instant
import java.util.UUID
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class KeystoreTokenStoreTest {
  private lateinit var context: Context
  private lateinit var directory: File
  private lateinit var store: KeystoreTokenStore
  private lateinit var alias: String
  private val token = StoredToken("t".repeat(43), Instant.parse("2030-01-01T00:00:00Z"))
  private val file get() = File(directory, "mobile-session.v1")

  @Before
  fun setUp() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "token-store-test-").toFile()
    val testPackage = "${app.packageName}.test.${UUID.randomUUID()}"
    // Keep real Android Keystore and files, isolated from any installed user's credentials.
    context = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir(): File = directory
      override fun getPackageName(): String = testPackage
    }
    alias = "$testPackage.mobile-session.v1"
    store = KeystoreTokenStore(context)
  }

  @After
  fun tearDown() {
    store.clear()
    directory.deleteRecursively()
  }

  @Test
  fun encryptedTokenSurvivesNewInstanceWithFreshIvAndNonExportableKey() {
    assertNull(store.read())
    store.save(token)
    val first = file.readBytes()
    val restored = KeystoreTokenStore(context).read()!!
    assertEquals(token.accessToken, restored.accessToken)
    assertEquals(token.expiresAt, restored.expiresAt)
    assertFalse(first.toString(Charsets.ISO_8859_1).contains(token.accessToken))
    assertNull(keys().getKey(alias, null).encoded)
    store.save(token)
    assertFalse(first.contentEquals(file.readBytes()))
    assertEquals(token.accessToken, store.read()!!.accessToken)
  }

  @Test
  fun verifiedPermitIsBoundToEncryptedTokenAndV1RemainsReadable() {
    store.save(token)
    assertNull(KeystoreTokenStore(context).read()!!.cacheAuthorization)
    val verifiedAt = token.expiresAt.minusSeconds(90L * 86_400)
    val permit = CacheAuthorization("u".repeat(43), verifiedAt, token.expiresAt)
    store.save(StoredToken(token.accessToken, token.expiresAt, permit))
    val restored = KeystoreTokenStore(context).read()!!
    val restoredPermit = restored.cacheAuthorization!!
    assertEquals(token.accessToken, restored.accessToken)
    assertEquals(permit.accountId, restoredPermit.accountId)
    assertEquals(verifiedAt, restoredPermit.verifiedAt)
    assertTrue(restoredPermit.permits(token.expiresAt.minusNanos(1)))
    assertFalse(restoredPermit.permits(token.expiresAt))
    assertFalse(restoredPermit.permits(verifiedAt.minusNanos(1)))
    assertFalse(file.readBytes().toString(Charsets.ISO_8859_1).contains(permit.accountId))
    assertThrows(IllegalArgumentException::class.java) {
      CacheAuthorization(permit.accountId, verifiedAt, token.expiresAt.plusSeconds(1))
    }
    assertThrows(IllegalArgumentException::class.java) {
      StoredToken(token.accessToken, token.expiresAt.minusSeconds(1), permit)
    }
    file.writeBytes(file.readBytes().apply { this[lastIndex] = (this[lastIndex].toInt() xor 1).toByte() })
    assertThrows(TokenStorageException::class.java) { store.read() }
  }

  @Test
  fun logoutIntentSurvivesRestartAndPreventsAReplacementLoginUntilTokenDeletion() {
    store.save(token)
    store.beginLogout()
    val reopened = KeystoreTokenStore(context)
    assertTrue(reopened.read()!!.logoutPending)
    assertThrows(TokenStorageException::class.java) { reopened.save(token) }
    reopened.clear()
    assertNull(reopened.read())
    assertFalse(File(directory, "mobile-session-logout.v1").exists())
    reopened.save(token)
    assertFalse(reopened.read()!!.logoutPending)
  }

  @Test
  fun tamperingAndInvalidEnvelopesFailClosedWithoutDisclosingContents() {
    store.save(token)
    val original = file.readBytes()
    val invalid = listOf(
      original.copyOf().apply { this[lastIndex] = (this[lastIndex].toInt() xor 1).toByte() },
      original.copyOf().apply { this[0] = 2 },
      original.copyOf(original.size - 1),
      original + 0.toByte(),
    )
    for (bytes in invalid) {
      file.writeBytes(bytes)
      val error = assertThrows(TokenStorageException::class.java) { store.read() }
      assertEquals("token_storage_unavailable", error.message)
      assertNull(error.cause)
      assertTrue(file.exists()) // Reading never silently discards or replaces credentials.
    }
    assertFalse(token.toString().contains(token.accessToken))
  }

  @Test
  fun missingKeyRequiresExplicitClearBeforeSavingANewLogin() {
    store.save(token)
    keys().deleteEntry(alias)
    assertThrows(TokenStorageException::class.java) { store.read() }
    assertThrows(TokenStorageException::class.java) { store.save(token) }
    assertFalse(keys().containsAlias(alias))
    store.clear()
    assertNull(store.read())
    store.save(token)
    assertEquals(token.accessToken, store.read()!!.accessToken)
  }

  @Test
  fun clearDeletesTokenAndDedicatedKeyButPreservesOtherAppFiles() {
    val unrelated = File(directory, "unrelated-state").apply { writeText("keep") }
    store.save(token)
    store.clear()
    store.clear()
    assertNull(KeystoreTokenStore(context).read())
    assertFalse(keys().containsAlias(alias))
    assertEquals(listOf(unrelated.name), directory.listFiles()!!.map { it.name })
  }

  @Test
  fun failedSaveDoesNotFallBackToPlaintextOrExposeTheToken() {
    val blocked = File(directory, "blocked-parent").apply { writeText("keep") }
    val blockedContext = object : ContextWrapper(context) {
      override fun getNoBackupFilesDir(): File = blocked
    }
    val error = assertThrows(TokenStorageException::class.java) {
      KeystoreTokenStore(blockedContext).save(token)
    }
    assertEquals("token_storage_unavailable", error.message)
    assertNull(error.cause)
    assertEquals("keep", blocked.readText())
    assertEquals(listOf(blocked.name), directory.listFiles()!!.map { it.name })
    store.save(token)
    assertEquals(token.accessToken, store.read()!!.accessToken)
  }

  @Test
  fun storageRemainsOutsideBackupsAndDoesNotAcceptMalformedCredentials() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    assertTrue(directory.canonicalPath.startsWith(app.noBackupFilesDir.canonicalPath + "/"))
    assertEquals(0, app.applicationInfo.flags and ApplicationInfo.FLAG_ALLOW_BACKUP)
    val error = assertThrows(IllegalArgumentException::class.java) {
      StoredToken("invalid-private-token", token.expiresAt)
    }
    assertEquals("invalid_stored_token", error.message)
  }

  private fun keys(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
}
