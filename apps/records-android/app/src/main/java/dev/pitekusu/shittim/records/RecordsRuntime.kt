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
  val enterDemo: () -> Unit = {},
  val exitDemo: () -> Unit = {},
  val resetDemo: () -> Unit = {},
) : ViewModelStoreOwner {
  override val viewModelStore = ViewModelStore()
  @Volatile private var active = true
  private val destination = MutableStateFlow<String?>(null)
  val demoDestination = destination.asStateFlow()

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
