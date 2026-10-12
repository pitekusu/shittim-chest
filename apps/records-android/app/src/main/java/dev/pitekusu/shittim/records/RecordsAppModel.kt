package dev.pitekusu.shittim.records

import android.app.Application
import android.content.Intent
import android.content.Context
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.StorageNamespace
import java.util.UUID
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.delay
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext

internal sealed interface RecordsAppState {
  data object Loading : RecordsAppState
  class Ready(val runtime: RecordsRuntime) : RecordsAppState
  data object StorageError : RecordsAppState
}

private sealed interface RecordsIncomingDestination {
  class Demo(val entry: String, val record: String) : RecordsIncomingDestination
  class Normal(val returnTo: String, val binding: String?) : RecordsIncomingDestination
}

/** Restores the mode before constructing any production client or authentication model. */
internal class RecordsAppModel @JvmOverloads constructor(application: Application,
  sessionFactory: (Context) -> MobileSessionModel = ::productionSession,
) : AndroidViewModel(application) {
  private val sessionFactory = sessionFactory
  private val context = application.applicationContext
  private val modes = ReviewDemoModeStore(context)
  private val normalModels = ViewModelStore()
  private var normalSession: MobileSessionModel? = null
  private var current: RecordsRuntime? = null
  private var operation: Job? = null
  private var demoProgress: Job? = null
  private var pendingDestination: RecordsIncomingDestination? = null
  private val mutable = MutableStateFlow<RecordsAppState>(RecordsAppState.Loading)
  val state = mutable.asStateFlow()

  init { recover() }

  fun onForeground() {
    if (ReviewDemoModeStore.productionAllowed(context)) normalSession?.onForeground()
  }

  fun recover() = runTransition {
    val mode = modes.read()
    if (current != null) stopRuntime()
    when (mode.phase) {
        ReviewDemoPhase.NORMAL -> showNormal()
        ReviewDemoPhase.DEMO -> showDemo(requireNotNull(mode.entryId))
        ReviewDemoPhase.ENTERING -> finishEntering()
        ReviewDemoPhase.EXITING -> finishExiting(reset = false)
        ReviewDemoPhase.RESETTING -> finishExiting(reset = true)
    }
  }

  fun enterDemo() {
    if (operation?.isActive == true || !ReviewDemoModeStore.productionAllowed(context)) return
    runTransition {
      writeMode(ReviewDemoMode(ReviewDemoPhase.ENTERING))
      stopRuntime()
      finishEntering()
    }
  }

  fun exitDemo(reset: Boolean = false) {
    val entry = current?.demoEntryId ?: return
    if (operation?.isActive == true || !modes.isDemo(entry)) return
    runTransition {
      writeMode(ReviewDemoMode(if (reset) ReviewDemoPhase.RESETTING else ReviewDemoPhase.EXITING, entry))
      stopRuntime()
      finishExiting(reset)
    }
  }

  private fun runTransition(action: suspend () -> Unit) {
    if (operation?.isActive == true) return
    mutable.value = RecordsAppState.Loading
    operation = viewModelScope.launch {
      try { action() }
      catch (error: CancellationException) { throw error }
      catch (_: Exception) { mutable.value = RecordsAppState.StorageError }
      finally { operation = null }
    }
  }

  private fun writeMode(mode: ReviewDemoMode) {
    // Serialize the mode change with the final notification/callback permission check.
    synchronized(RecordNotifications.lock) { modes.write(mode) }
  }

  private suspend fun stopRuntime() {
    val old = current
    current = null
    old?.deactivate()
    demoProgress?.cancelAndJoin()
    demoProgress = null
    RecordSyncScheduler.cancelAndAwait(context)
    RecordNotificationRegistration.cancelAndAwait(context)
    if (old?.demo != null) withContext(Dispatchers.IO) {
      RecordCacheAccount.lock.withLock { old.demo.close() }
    }
  }

  private suspend fun finishEntering() {
    stopRuntime()
    val session = normalSession
    if (session != null) session.logoutForDemo()
    else eraseNormalStorage()
    normalModels.clear()
    normalSession = null
    // No surviving cached record, draft or notification may be unlocked by demo data.
    RecordCacheAccount.clear(context, StorageNamespace.DEMO)
    DemoNotifications.clear(context)
    val entry = UUID.randomUUID().toString()
    writeMode(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
    showDemo(entry)
  }

  private suspend fun eraseNormalStorage() {
    // Startup recovery must not create a MobileSessionModel (its init checks the server).
    RecordNotifications.revoke(context)
    withContext(NonCancellable + Dispatchers.IO) {
      val store = KeystoreTokenStore(context)
      store.beginLogout()
      try { RecordCacheAccount.clear(context) }
      finally { store.clear() }
      store.completeLogout()
    }
  }

  private suspend fun finishExiting(reset: Boolean) {
    stopRuntime()
    DemoNotifications.clear(context)
    RecordCacheAccount.clear(context, StorageNamespace.DEMO)
    if (reset) {
      val entry = UUID.randomUUID().toString()
      writeMode(ReviewDemoMode(ReviewDemoPhase.DEMO, entry))
      showDemo(entry)
    } else {
      writeMode(ReviewDemoMode(ReviewDemoPhase.NORMAL))
      showNormal()
    }
  }

  private fun showNormal() {
    val session = ViewModelProvider(normalModels, viewModelFactory {
      initializer { sessionFactory(context) }
    })[MobileSessionModel::class.java]
    normalSession = session
    current = RecordsRuntime(session = session,
      productionAllowed = { ReviewDemoModeStore.productionAllowed(context) }, enterDemo = ::enterDemo)
    mutable.value = RecordsAppState.Ready(requireNotNull(current))
    deliverPendingIntent()
  }

  private suspend fun showDemo(entry: String) {
    RecordCacheAccount.activate(context, DEMO_ACCOUNT_ID, StorageNamespace.DEMO)
    val sandbox = DemoSandbox(context, entry, onPublished = { record ->
      DemoNotifications.show(context, entry, record, "デモ利用者")
    })
    current = RecordsRuntime(demo = sandbox, demoEntryId = entry,
      exitDemo = { exitDemo() }, resetDemo = { exitDemo(reset = true) })
    mutable.value = RecordsAppState.Ready(requireNotNull(current))
    // Local generation continues after navigating Back or putting the app in the background.
    // Process death is recovered from the encrypted request when the app starts again.
    demoProgress = viewModelScope.launch {
      while (sandbox.isActive()) {
        try { sandbox.list() }
        catch (error: CancellationException) { throw error }
        catch (_: Exception) { /* The normal retry UI exposes storage failures. */ }
        delay(1_000)
      }
    }
    deliverPendingIntent()
  }

  fun openIntent(intent: Intent?) {
    // Extract only the supported action and scalar values; never forward a supplied Intent.
    pendingDestination = try {
      when (intent?.action) {
        REVIEW_DEMO_NOTIFICATION_ACTION -> {
          val entry = intent.getStringExtra(REVIEW_DEMO_NOTIFICATION_ENTRY)
          val record = intent.getStringExtra(REVIEW_DEMO_NOTIFICATION_RECORD)
          if (entry != null && record != null && validDebateRequestId(entry) &&
            dev.pitekusu.shittim.records.auth.mobileOpaqueValue.matches(record))
            RecordsIncomingDestination.Demo(entry, record) else null
        }
        Intent.ACTION_VIEW -> {
          val route = recordDestination(intent.dataString)
          val binding = intent.getStringExtra(RECORD_NOTIFICATION_BINDING)
          if (route != null && ReviewDemoModeStore.productionAllowed(context) &&
            (binding == null || dev.pitekusu.shittim.records.auth.mobileOpaqueValue.matches(binding)))
            RecordsIncomingDestination.Normal(route, binding) else null
        }
        else -> null
      }
    } catch (_: RuntimeException) { null } // Malformed parcelable extras cannot crash an exported Activity.
    if (mutable.value is RecordsAppState.Ready) deliverPendingIntent()
  }

  private fun deliverPendingIntent() {
    val destination = pendingDestination ?: return
    pendingDestination = null
    val runtime = current ?: return
    if (destination is RecordsIncomingDestination.Demo) {
      val entry = destination.entry
      val record = destination.record
      if (runtime.demoEntryId != entry || !modes.isDemo(entry) ||
          !dev.pitekusu.shittim.records.auth.mobileOpaqueValue.matches(record)) return
      viewModelScope.launch {
        try {
          if (runtime.demo?.cachedRecord(DEMO_ACCOUNT_ID, record) != null && current === runtime)
            runtime.openDemoRecord(record)
        } catch (error: CancellationException) { throw error }
        catch (_: Exception) { /* Invalid or stale notifications never grant access. */ }
      }
    } else if (destination is RecordsIncomingDestination.Normal && ReviewDemoModeStore.productionAllowed(context)) {
      destination.binding?.let { binding ->
        if (binding != RecordNotificationSettings(context).binding || !hasLocalNotificationPermit(context)) return
      }
      runtime.session?.openDestination(destination.returnTo)
    }
  }

  override fun onCleared() {
    demoProgress?.cancel()
    current?.deactivate()
    current?.demo?.close()
    normalModels.clear()
    pendingDestination = null
  }
}

private fun productionSession(context: Context): MobileSessionModel {
  val store = KeystoreTokenStore(context)
  return MobileSessionModel(MobileAuthClient(), store::read, store::save, store::clear,
    store::invalidateCacheAuthorization, store::beginLogout, store::isLogoutPending,
    store::completeLogout, { RecordCacheAccount.activate(context, it) },
    { RecordSyncScheduler.cancelAndAwait(context); RecordCacheAccount.clear(context) },
    revokeNotifications = { RecordNotifications.revoke(context) })
}
