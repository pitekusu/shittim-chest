package dev.pitekusu.shittim.records

import android.content.Context
import android.util.AtomicFile
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.File
import java.io.FileNotFoundException

// A fictional local owner, never a credential or server-issued account identifier.
internal const val DEMO_ACCOUNT_ID = "review_demo_0000000000000000000000000000000"

internal enum class ReviewDemoPhase { NORMAL, ENTERING, DEMO, EXITING, RESETTING }

internal data class ReviewDemoMode(val phase: ReviewDemoPhase, val entryId: String? = null)

internal class ReviewDemoModeException : Exception("review_demo_mode_unavailable")

/** Read before creating production clients, including background workers and login callbacks. */
internal class ReviewDemoModeStore(context: Context) {
  private val file = AtomicFile(File(context.noBackupFilesDir, FILE_NAME))

  init { require(!context.isDeviceProtectedStorage) }

  fun read(): ReviewDemoMode = guarded { readLocked() }

  fun write(mode: ReviewDemoMode): Unit = guarded {
    validate(mode)
    val output = file.startWrite()
    try {
      val data = DataOutputStream(output)
      data.writeByte(VERSION)
      data.writeByte(phaseCode(mode.phase))
      data.writeBoolean(mode.entryId != null)
      mode.entryId?.let(data::writeUTF)
      data.flush()
      file.finishWrite(output)
      check(readLocked() == mode)
      generation++
    } catch (error: Exception) {
      file.failWrite(output)
      throw error
    }
  }

  fun isDemo(entryId: String): Boolean = try {
    val mode = read()
    mode.phase == ReviewDemoPhase.DEMO && mode.entryId == entryId
  } catch (_: ReviewDemoModeException) { false }

  private fun readLocked(): ReviewDemoMode {
    // AtomicFile discards an interrupted new write or restores its committed backup.
    // Transition cleanup starts only after write/readback succeeds, so restoring the
    // previous mode never authorizes data whose cleanup had already begun.
    val input = try { file.openRead() }
    catch (error: FileNotFoundException) {
      if (listOf("", ".bak").any { File(file.baseFile.path + it).exists() }) throw error
      // AtomicFile retains .new when its first-ever write has no committed base.
      // No transition cleanup can have begun; recover this uncommitted first write.
      val uncommitted = File(file.baseFile.path + ".new")
      if (uncommitted.exists()) check(uncommitted.isFile && uncommitted.delete())
      return ReviewDemoMode(ReviewDemoPhase.NORMAL)
    }
    return input.use {
      check(input.channel.size() in 3..41)
      val data = DataInputStream(input)
      check(data.readUnsignedByte() == VERSION)
      val phase = when (data.readUnsignedByte()) {
        0 -> ReviewDemoPhase.NORMAL
        1 -> ReviewDemoPhase.ENTERING
        2 -> ReviewDemoPhase.DEMO
        3 -> ReviewDemoPhase.EXITING
        4 -> ReviewDemoPhase.RESETTING
        else -> error("invalid_mode")
      }
      val hasEntry = data.readUnsignedByte().also { check(it in 0..1) } == 1
      ReviewDemoMode(phase, if (hasEntry) data.readUTF() else null).also { mode ->
        check(data.read() == -1)
        validate(mode)
      }
    }
  }

  private fun validate(mode: ReviewDemoMode) {
    mode.entryId?.let { check(validDebateRequestId(it)) }
    if (mode.phase == ReviewDemoPhase.NORMAL) check(mode.entryId == null)
    if (mode.phase in setOf(ReviewDemoPhase.DEMO, ReviewDemoPhase.EXITING, ReviewDemoPhase.RESETTING))
      check(mode.entryId != null)
  }

  private fun phaseCode(phase: ReviewDemoPhase): Int = when (phase) {
    ReviewDemoPhase.NORMAL -> 0
    ReviewDemoPhase.ENTERING -> 1
    ReviewDemoPhase.DEMO -> 2
    ReviewDemoPhase.EXITING -> 3
    ReviewDemoPhase.RESETTING -> 4
  }

  private fun <T> guarded(action: () -> T): T = synchronized(lock) {
    try { action() } catch (_: Exception) { throw ReviewDemoModeException() }
  }

  companion object {
    private val lock = Any()
    private var generation = 0L
    private const val VERSION = 1
    private const val FILE_NAME = "review-demo-mode.v1"

    /** A normal-to-demo-to-normal round trip cannot revive an older in-process login callback. */
    fun productionEpoch(context: Context): Long? = try {
      synchronized(lock) {
        if (ReviewDemoModeStore(context).readLocked().phase == ReviewDemoPhase.NORMAL) generation else null
      }
    } catch (_: Exception) { null }

    fun productionAllowed(context: Context): Boolean = productionEpoch(context) != null
  }
}
