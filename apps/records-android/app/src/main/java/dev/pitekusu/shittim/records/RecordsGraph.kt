package dev.pitekusu.shittim.records

import com.slack.circuit.foundation.Circuit
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.zacsweers.metro.DependencyGraph
import dev.zacsweers.metro.Provides

// Created once per Activity. Only the dependencies used by the current screen are wired here.
@DependencyGraph
internal interface RecordsGraph {
  val circuit: Circuit

  @DependencyGraph.Factory
  fun interface Factory {
    fun create(@Provides session: MobileSessionModel): RecordsGraph
  }

  @Provides
  fun provideCircuit(presenter: BootstrapPresenter): Circuit =
    Circuit.Builder()
      .addPresenter<BootstrapScreen, BootstrapScreen.State>(presenter)
      .addUi<BootstrapScreen, BootstrapScreen.State> { state, modifier ->
        BootstrapUi(state, modifier)
      }
      .build()
}
