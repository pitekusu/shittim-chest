package dev.pitekusu.shittim.records

import androidx.navigation3.runtime.NavBackStack
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.serialization.NavBackStackSerializer
import androidx.navigation3.runtime.serialization.NavKeySerializer
import androidx.savedstate.serialization.decodeFromSavedState
import androidx.savedstate.serialization.encodeToSavedState
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.work.testing.TestListenableWorkerBuilder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** Framework boundaries that discover routes, persisted workers and Room code at runtime. */
@RunWith(AndroidJUnit4::class)
class RuntimeDiscoveryTest {
  @Test fun everyRouteSurvivesTheSavedStateSerializerUsedByRememberNavBackStack() {
    val original = NavBackStack<NavKey>(RecordsList, Insights, DebateCompose, DebateRequests,
      DebateRequestStatus("00000000-0000-4000-8000-000000000000"), RecordDetail("a".repeat(43)))
    // Concrete route serializers are intentionally not supplied here. Navigation 3 resolves them
    // from runtime class information, including objects and classes with a companion serializer.
    val serializer = NavBackStackSerializer(NavKeySerializer<NavKey>())
    val savedState = encodeToSavedState(serializer, original)
    val restored = decodeFromSavedState(serializer, savedState)
    assertEquals(original.toList(), restored.toList())
  }

  @Test fun defaultWorkerFactoryCanRecreatePersistedWorkersByReflection() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    // No custom factory or doWork(): verify the production constructor boundary without networking.
    val workers = listOf(
      TestListenableWorkerBuilder<RecordSyncWorker>(context).build(),
      TestListenableWorkerBuilder<RecordNotificationWorker>(context).build(),
      TestListenableWorkerBuilder<RecordNotificationRecoveryWorker>(context).build(),
    )
    assertEquals(listOf("dev.pitekusu.shittim.records.RecordSyncWorker",
      "dev.pitekusu.shittim.records.RecordNotificationWorker",
      "dev.pitekusu.shittim.records.RecordNotificationRecoveryWorker"), workers.map { it.javaClass.name })
  }

  @Test fun generatedRoomDatabaseKeepsItsReflectiveConstructor() {
    val context = InstrumentationRegistry.getInstrumentation().targetContext
    val implementation = Class.forName(
      "dev.pitekusu.shittim.records.storage.EncryptedRecordsDatabase_Impl", true, context.classLoader)
    assertTrue(implementation.getDeclaredConstructor().newInstance() is androidx.room3.RoomDatabase)
  }
}
