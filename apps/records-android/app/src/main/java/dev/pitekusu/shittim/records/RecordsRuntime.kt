package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModelStore
import androidx.lifecycle.ViewModelStoreOwner
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

/** Composition selects capabilities, never a fabricated authenticated session. */
internal class RecordsRuntime(
  val session: MobileSessionModel? = null,
  val demo: DemoSandbox? = null,
  val demoEntryId: String? = null,
  private val productionAllowed: () -> Boolean = { true },
  enterDemo: () -> Unit = {},
  exitDemo: () -> Unit = {},
  resetDemo: () -> Unit = {},
) : ViewModelStoreOwner {
  override val viewModelStore = ViewModelStore()
  @Volatile private var active = true
  private val enterDemoAction = enterDemo
  private val exitDemoAction = exitDemo
  private val resetDemoAction = resetDemo
  // Retained UI callbacks belong to this runtime and cannot act on its replacement.
  val enterDemo: () -> Unit = { if (active && demo == null && productionAllowed()) enterDemoAction() }
  val exitDemo: () -> Unit = { if (active && demo?.isActive() == true) exitDemoAction() }
  val resetDemo: () -> Unit = { if (active && demo?.isActive() == true) resetDemoAction() }
  private val destination = MutableStateFlow<String?>(null)
  val demoDestination = destination.asStateFlow()

  fun openPublicPage(page: RecordsPublicPage, launch: (RecordsPublicPage) -> Unit) {
    if (!active || (if (demo != null) !demo.isActive() else !productionAllowed())) return
    launch(page)
  }

  fun isActive(accountId: String): Boolean = active && when {
    demo != null -> accountId == DEMO_ACCOUNT_ID && demo.isActive()
    else -> productionAllowed() && session?.isCacheAuthorized(accountId) == true
  }

  fun openDemoRecord(recordId: String) { if (isActive(DEMO_ACCOUNT_ID)) destination.value = recordId }
  fun consumeDemoRecord(recordId: String): Boolean {
    if (!isActive(DEMO_ACCOUNT_ID) || destination.value != recordId) return false
    destination.value = null
    return true
  }

  fun deactivate() {
    active = false
    destination.value = null
    viewModelStore.clear()
  }
}
