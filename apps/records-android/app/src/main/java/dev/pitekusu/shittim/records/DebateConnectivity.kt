package dev.pitekusu.shittim.records

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.flow.distinctUntilChanged

internal fun hasValidatedDebateNetwork(context: Context): Boolean = try {
  val manager = context.getSystemService(ConnectivityManager::class.java)
  val capabilities = manager?.getNetworkCapabilities(manager.activeNetwork)
  capabilities?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) == true &&
    capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
} catch (_: SecurityException) { false }

/** Standard network callbacks are collected only while this activity is in the foreground. */
@Composable
internal fun rememberValidatedDebateNetwork(): Boolean {
  val context = LocalContext.current.applicationContext
  val changes = remember(context) { callbackFlow {
    val manager = context.getSystemService(ConnectivityManager::class.java)
    if (manager == null) { trySend(false); close(); return@callbackFlow }
    val callback = object : ConnectivityManager.NetworkCallback() {
      override fun onAvailable(network: Network) { trySend(hasValidatedDebateNetwork(context)) }
      override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
        trySend(hasValidatedDebateNetwork(context))
      }
      override fun onLost(network: Network) { trySend(hasValidatedDebateNetwork(context)) }
    }
    try {
      manager.registerDefaultNetworkCallback(callback)
      trySend(hasValidatedDebateNetwork(context))
    } catch (_: SecurityException) { trySend(false); close(); return@callbackFlow }
    awaitClose { manager.unregisterNetworkCallback(callback) }
  }.distinctUntilChanged() }
  val validated by changes.collectAsStateWithLifecycle(initialValue = hasValidatedDebateNetwork(context),
    minActiveState = Lifecycle.State.RESUMED)
  return validated
}
