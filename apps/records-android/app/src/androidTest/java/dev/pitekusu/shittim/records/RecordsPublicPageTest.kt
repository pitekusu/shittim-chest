package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.os.Bundle
import android.provider.Browser
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.StorageNamespace
import java.io.File
import java.nio.file.Files
import java.util.UUID
import java.util.Locale
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordsPublicPageTest {
  private lateinit var context: Context
  private lateinit var directory: File
  private val intents = mutableListOf<Intent>()
  private var sandbox: DemoSandbox? = null

  @Before fun prepare() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "public-page-test-").toFile()
    val testPackage = "${app.packageName}.public.${UUID.randomUUID()}"
    context = object : ContextWrapper(app) {
      override fun getApplicationContext(): Context = this
      override fun getNoBackupFilesDir(): File = directory
      override fun getDatabasePath(name: String): File = File(directory, name)
      override fun getPackageName(): String = testPackage
      override fun startActivity(intent: Intent) { intents += Intent(intent) }
      override fun startActivity(intent: Intent, options: Bundle?) { intents += Intent(intent) }
    }
  }

  @After fun cleanup() = runBlocking {
    sandbox?.close()
    RecordCacheAccount.clear(context, StorageNamespace.DEMO)
    directory.deleteRecursively()
    Unit
  }

  @Test fun publicPagesDispatchOnlyTheTwoAnonymousHttpsUrlsWithoutAuthenticationHeaders() {
    val runtime = RecordsRuntime()
    RecordsPublicPage.entries.forEach { page ->
      runtime.openPublicPage(page) { assertTrue(launchRecordsPublicPage(context, it)) }
    }
    assertEquals(listOf("https://shittim.pitekusu.dev/privacy-policy",
      "https://shittim.pitekusu.dev/data-deletion"), intents.map { it.dataString })
    assertBrowserIntentsHaveNoNativeCredentials()
    runtime.deactivate()
  }

  @Test fun normalFeaturePagesDispatchOnlyFixedHttpsUrlsWithoutNativeCredentials() {
    val runtime = RecordsRuntime()
    RecordsWebPage.entries.forEach { page ->
      runtime.openWebPage(page) { assertTrue(launchRecordsWebPage(context, it)) }
    }
    assertEquals(listOf("https://shittim.pitekusu.dev/insights", "https://shittim.pitekusu.dev/momotalk",
      "https://shittim.pitekusu.dev/memorial", "https://shittim.pitekusu.dev/admin",
      "https://shittim.pitekusu.dev/admin/prompts"), intents.map { it.dataString })
    assertBrowserIntentsHaveNoNativeCredentials()
    runtime.deactivate()
  }

  private fun assertBrowserIntentsHaveNoNativeCredentials() {
    intents.forEach { intent ->
      assertEquals(Intent.ACTION_VIEW, intent.action)
      assertNull(intent.data?.query)
      assertNull(intent.data?.fragment)
      // Browser 1.10 adds the locale's Accept-Language header in Builder.build().
      // Browser navigation must not add native credentials alongside those SDK defaults.
      val headers = intent.getBundleExtra(Browser.EXTRA_HEADERS)
      headers?.let { values ->
        values.keySet().forEach { name ->
          val normalized = name.lowercase(Locale.ROOT)
          assertFalse(normalized in setOf("authorization", "proxy-authorization", "x-api-key"))
          assertFalse(normalized.contains("cookie") || normalized.contains("token"))
          assertFalse(values.getString(name).orEmpty().startsWith("Bearer ", ignoreCase = true))
        }
      }
      val credentialExtras = setOf("authorization", "cookie", "accesstoken", "refreshtoken", "csrftoken")
      assertFalse(intent.extras?.keySet().orEmpty().any { name ->
        name.substringAfterLast('.').lowercase(Locale.ROOT) in credentialExtras
      })
    }
  }

  @Test fun inactiveAndBlockedNormalRuntimeCannotLaunchARetainedPublicPageAction() {
    var allowed = true
    val runtime = RecordsRuntime(productionAllowed = { allowed })
    val retained = { runtime.openPublicPage(RecordsPublicPage.PrivacyPolicy) {
      launchRecordsPublicPage(context, it)
    } }
    retained()
    assertEquals(1, intents.size)
    allowed = false
    retained()
    assertEquals(1, intents.size)
    allowed = true
    runtime.deactivate()
    retained()
    assertEquals(1, intents.size)
  }

  @Test fun inactiveAndBlockedNormalRuntimeCannotLaunchARetainedFeaturePageAction() {
    var allowed = true
    val runtime = RecordsRuntime(productionAllowed = { allowed })
    val retained = { runtime.openWebPage(RecordsWebPage.Momotalk) {
      launchRecordsWebPage(context, it)
    } }
    retained()
    assertEquals(1, intents.size)
    allowed = false
    retained()
    assertEquals(1, intents.size)
    allowed = true
    runtime.deactivate()
    retained()
    assertEquals(1, intents.size)
  }

  @Test fun demoMayLaunchAPublicPageButItsOldEntryCannotLaunchAfterReset() {
    val modes = ReviewDemoModeStore(context)
    val entry = UUID.randomUUID().toString()
    modes.write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    val demo = DemoSandbox(context, entry).also { sandbox = it }
    val runtime = RecordsRuntime(demo = demo, demoEntryId = entry)
    RecordsWebPage.entries.forEach { page ->
      runtime.openWebPage(page) { launchRecordsWebPage(context, it) }
    }
    assertTrue(intents.isEmpty())
    val retained = { runtime.openPublicPage(RecordsPublicPage.DataDeletion) {
      launchRecordsPublicPage(context, it)
    } }
    retained()
    assertEquals("https://shittim.pitekusu.dev/data-deletion", intents.single().dataString)
    modes.write(ReviewDemoMode(ReviewDemoPhase.DEMO, UUID.randomUUID().toString()))
    retained()
    assertEquals(1, intents.size)
    runtime.deactivate()
  }
}
