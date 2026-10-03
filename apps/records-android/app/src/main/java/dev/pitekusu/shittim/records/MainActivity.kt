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
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.auth.MobileAuthClient
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.zacsweers.metro.createGraphFactory

class MainActivity : ComponentActivity() {
  private val session by viewModels<MobileSessionModel> {
    viewModelFactory {
      initializer {
        val store = KeystoreTokenStore(applicationContext)
        MobileSessionModel(MobileAuthClient(), store::read, store::save, store::clear,
          store::invalidateCacheAuthorization, store::beginLogout,
          store::isLogoutPending, store::completeLogout,
          { accountId -> RecordCacheAccount.activate(applicationContext, accountId) },
          {
            RecordSyncScheduler.cancel(applicationContext)
            RecordCacheAccount.clear(applicationContext)
          }, revokeNotifications = { RecordNotifications.revoke(applicationContext) })
      }
    }
  }
  private val graph by lazy { createGraphFactory<RecordsGraph.Factory>().create(session) }

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
      CompositionLocalProvider(LocalStartupIntro provides firstLaunch) {
        CircuitContent(screen = BootstrapScreen, circuit = graph.circuit)
      }
    }
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
      // A PendingIntent left from an earlier login must not select a record for another account.
      intent.getStringExtra(RECORD_NOTIFICATION_BINDING)?.let { binding ->
        if (binding != RecordNotificationSettings(applicationContext).binding ||
          !hasLocalNotificationPermit(applicationContext)) return
      }
      recordDestination(intent.dataString)?.let(session::openDestination)
    }
  }
}
