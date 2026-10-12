package dev.pitekusu.shittim.records

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.compose.runtime.CompositionLocalProvider
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.slack.circuit.foundation.CircuitContent
import coil3.ImageLoader
import coil3.SingletonImageLoader
import coil3.request.CachePolicy
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.remember
import androidx.lifecycle.viewmodel.compose.LocalViewModelStoreOwner
import dev.zacsweers.metro.createGraphFactory

class MainActivity : ComponentActivity() {
  private val app by viewModels<RecordsAppModel>()

  override fun onCreate(savedInstanceState: Bundle?) {
    installSplashScreen()
    super.onCreate(savedInstanceState)
    // Presigned requester avatars must not become plaintext files on the device.
    SingletonImageLoader.setSafe { context ->
      ImageLoader.Builder(context).diskCachePolicy(CachePolicy.DISABLED).build()
    }
    // A recreated Activity must not replay an App Link the user already closed.
    if (savedInstanceState == null) openRecordLink(intent)
    enableEdgeToEdge()
    val firstLaunch = savedInstanceState == null
    setContent {
      val state by app.state.collectAsState()
      when (val ready = state) {
        is RecordsAppState.Ready -> key(ready.runtime.demoEntryId ?: "normal") {
          val graph = remember(ready.runtime) {
            createGraphFactory<RecordsGraph.Factory>().create(ready.runtime)
          }
          CompositionLocalProvider(LocalStartupIntro provides firstLaunch,
            LocalViewModelStoreOwner provides ready.runtime) {
            CircuitContent(screen = BootstrapScreen, circuit = graph.circuit)
          }
        }
        else -> ReviewDemoTransitionUi(state == RecordsAppState.StorageError, app::recover)
      }
    }
  }

  override fun onNewIntent(intent: Intent) {
    super.onNewIntent(intent)
    openRecordLink(intent)
  }

  override fun onResume() {
    super.onResume()
    app.onForeground()
  }

  private fun openRecordLink(intent: Intent?) {
    app.openIntent(intent)
  }
}
