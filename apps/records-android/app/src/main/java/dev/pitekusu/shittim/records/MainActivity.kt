package dev.pitekusu.shittim.records

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import com.slack.circuit.foundation.CircuitContent
import dev.zacsweers.metro.createGraph

class MainActivity : ComponentActivity() {
  private val graph by lazy { createGraph<RecordsGraph>() }

  override fun onCreate(savedInstanceState: Bundle?) {
    super.onCreate(savedInstanceState)
    enableEdgeToEdge()
    setContent { CircuitContent(screen = BootstrapScreen, circuit = graph.circuit) }
  }
}
