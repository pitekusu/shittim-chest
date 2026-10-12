package dev.pitekusu.shittim.records

import android.content.Context
import android.content.ContextWrapper
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.nio.file.Files
import java.util.UUID
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ReviewDemoModeStoreTest {
  private lateinit var directory: File
  private lateinit var context: Context

  @Before fun setup() {
    val app = InstrumentationRegistry.getInstrumentation().targetContext
    directory = Files.createTempDirectory(app.noBackupFilesDir.toPath(), "review-mode-test-").toFile()
    context = object : ContextWrapper(app) {
      override fun getNoBackupFilesDir() = directory
    }
  }

  @After fun cleanup() { directory.deleteRecursively() }

  @Test fun restoredDemoHasNoDeadlineAndTransitionsNeverAuthorizeProduction() {
    val entry = UUID.randomUUID().toString()
    val other = UUID.randomUUID().toString()
    val store = ReviewDemoModeStore(context)
    assertEquals(ReviewDemoMode(ReviewDemoPhase.NORMAL), store.read())
    assertTrue(ReviewDemoModeStore.productionAllowed(context))
    store.write(ReviewDemoMode(ReviewDemoPhase.ENTERING))
    assertFalse(ReviewDemoModeStore.productionAllowed(context))
    store.write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    val reopened = ReviewDemoModeStore(context)
    assertEquals(ReviewDemoMode(ReviewDemoPhase.DEMO, entry), reopened.read())
    assertTrue(reopened.isDemo(entry))
    assertFalse(reopened.isDemo(other))
    assertFalse(ReviewDemoModeStore.productionAllowed(context))
    for (phase in listOf(ReviewDemoPhase.RESETTING, ReviewDemoPhase.EXITING)) {
      reopened.write(ReviewDemoMode(phase, entry))
      assertEquals(phase, ReviewDemoModeStore(context).read().phase)
      assertFalse(reopened.isDemo(entry))
      assertFalse(ReviewDemoModeStore.productionAllowed(context))
    }
    reopened.write(ReviewDemoMode(ReviewDemoPhase.DEMO, other))
    assertFalse(store.isDemo(entry))
    assertTrue(store.isDemo(other))
    reopened.write(ReviewDemoMode(ReviewDemoPhase.NORMAL))
    assertTrue(ReviewDemoModeStore.productionAllowed(context))
  }

  @Test fun malformedCommittedModeFilesNeverRestoreProductionAccess() {
    val file = File(directory, "review-demo-mode.v1")
    val store = ReviewDemoModeStore(context)
    for (bytes in listOf(byteArrayOf(), byteArrayOf(2, 0, 0), byteArrayOf(1, 99, 0),
        byteArrayOf(1, 2, 0), byteArrayOf(1, 0, 2), byteArrayOf(1, 0, 0, 0))) {
      file.writeBytes(bytes)
      failure { store.read() }
      assertFalse(ReviewDemoModeStore.productionAllowed(context))
      assertFalse(store.isDemo(UUID.randomUUID().toString()))
    }
  }

  @Test fun interruptedAtomicWritesRestoreOnlyTheLastCommittedTransition() {
    val entry = UUID.randomUUID().toString()
    val store = ReviewDemoModeStore(context)
    val file = File(directory, "review-demo-mode.v1")
    for (mode in listOf(ReviewDemoMode(ReviewDemoPhase.NORMAL), ReviewDemoMode(ReviewDemoPhase.ENTERING),
        ReviewDemoMode(ReviewDemoPhase.DEMO, entry), ReviewDemoMode(ReviewDemoPhase.EXITING, entry),
        ReviewDemoMode(ReviewDemoPhase.RESETTING, entry))) {
      store.write(mode)
      File(file.path + ".new").writeBytes(byteArrayOf(1, 4))
      assertEquals(mode, ReviewDemoModeStore(context).read())
      assertEquals(mode.phase == ReviewDemoPhase.NORMAL, ReviewDemoModeStore.productionAllowed(context))
      assertEquals(mode.phase == ReviewDemoPhase.DEMO, store.isDemo(entry))
    }
    store.write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    File(file.path + ".bak").writeBytes(file.readBytes())
    file.writeBytes(byteArrayOf(99))
    assertEquals(ReviewDemoMode(ReviewDemoPhase.DEMO, entry), store.read())
    assertFalse(ReviewDemoModeStore.productionAllowed(context))
    assertTrue(file.delete())
    // No transition was committed, so the caller has not started cleanup or opened demo data.
    File(file.path + ".new").writeBytes(byteArrayOf(1, 1))
    assertEquals(ReviewDemoMode(ReviewDemoPhase.NORMAL), store.read())
  }

  @Test fun invalidEntryCannotReplaceTheCommittedMode() {
    val entry = UUID.randomUUID().toString()
    val store = ReviewDemoModeStore(context)
    store.write(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    for (mode in listOf(ReviewDemoMode(ReviewDemoPhase.NORMAL, entry),
        ReviewDemoMode(ReviewDemoPhase.DEMO), ReviewDemoMode(ReviewDemoPhase.EXITING, "not-a-uuid"))) {
      failure { store.write(mode) }
      assertEquals(ReviewDemoMode(ReviewDemoPhase.DEMO, entry), store.read())
    }
    assertEquals(43, DEMO_ACCOUNT_ID.length)
  }

  @Test fun productionEpochRejectsCallbacksFromBeforeADemoRoundTrip() {
    val store = ReviewDemoModeStore(context)
    val before = requireNotNull(ReviewDemoModeStore.productionEpoch(context))
    store.write(ReviewDemoMode(ReviewDemoPhase.DEMO, UUID.randomUUID().toString()))
    assertNull(ReviewDemoModeStore.productionEpoch(context))
    store.write(ReviewDemoMode(ReviewDemoPhase.NORMAL))
    val after = requireNotNull(ReviewDemoModeStore.productionEpoch(context))
    assertTrue(after > before)
    assertNotEquals(before, after)
  }

  private fun failure(action: () -> Any?) {
    try { action(); fail("mode storage must fail closed") }
    catch (error: ReviewDemoModeException) {
      assertEquals("review_demo_mode_unavailable", error.message)
      assertNull(error.cause)
    }
  }
}
