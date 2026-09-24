package dev.pitekusu.shittim.records

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.slack.circuit.foundation.CircuitContent
import coil3.ImageLoader
import coil3.SingletonImageLoader
import coil3.request.CachePolicy
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.zacsweers.metro.createGraphFactory

class MainActivity : ComponentActivity() {
  private val session by viewModels<MobileSessionModel> {
    viewModelFactory {
      initializer {
        val store = KeystoreTokenStore(applicationContext)
        MobileSessionModel(MobileAuthClient(), store::read, store::clear)
      }
    }
  }
  private val graph by lazy { createGraphFactory<RecordsGraph.Factory>().create(session) }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    // Presigned requester avatars must not become plaintext files on the device.
    SingletonImageLoader.setSafe { context ->
      ImageLoader.Builder(context).diskCachePolicy(CachePolicy.DISABLED).build()
    }
    openRecordLink(intent)
    enableEdgeToEdge()
    setContent { CircuitContent(screen = BootstrapScreen, circuit = graph.circuit) }
  }

  override fun onNewIntent(intent: Intent) {
    super.onNewIntent(intent)
    openRecordLink(intent)
  }

  override fun onResume() {
    super.onResume()
    session.onForeground()
  }

  private fun openRecordLink(intent: Intent?) {
    if (intent?.action == Intent.ACTION_VIEW) {
      recordDestination(intent.dataString)?.let(session::openDestination)
    }
  }
}
