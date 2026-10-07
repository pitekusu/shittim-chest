import java.io.File
import java.nio.channels.FileChannel
import java.nio.file.Files
import java.nio.file.StandardOpenOption
import org.gradle.api.GradleException

val buildRoot = gradle.startParameter.projectProperties["shittimAndroidBuildRoot"]
  ?.let(::File)
  ?.takeIf { it.isAbsolute }
  ?: throw GradleException("shittimAndroidBuildRoot must be an absolute path")

// --no-daemon can still fork a detached single-use JVM. Keep the scratch lease
// until that JVM shuts down, not merely until Gradle's buildFinished event.
val lifetimeChannel = FileChannel.open(
  buildRoot.parentFile.resolve("lifetime.lock").toPath(), StandardOpenOption.WRITE,
)
val lifetimeLock = lifetimeChannel.lock()
// Hooks run concurrently. Retain both objects until all hooks finish; explicit
// release here would let Python delete files while another shutdown hook uses them.
// The OS releases this lease only when the JVM process actually exits.
Runtime.getRuntime().addShutdownHook(object : Thread("shittim-scratch-lease") {
  private val retainedLock = lifetimeLock
  private val retainedChannel = lifetimeChannel
  override fun run() {
    retainedLock.isValid && retainedChannel.isOpen
  }
})
Files.createFile(buildRoot.parentFile.resolve("lifetime.started").toPath())

gradle.beforeProject {
  val directory = if (path == ":") "_root" else path.removePrefix(":").replace(':', '/')
  layout.buildDirectory.set(buildRoot.resolve(directory))
}
