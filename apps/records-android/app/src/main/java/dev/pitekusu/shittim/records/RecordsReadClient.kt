package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.RECORDS_ORIGIN
import dev.pitekusu.shittim.records.auth.mobileOpaqueValue
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.accept
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import java.io.Closeable
import java.io.IOException
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.CookieJar

internal enum class RecordReadFailure { AUTH_REQUIRED, NOT_FOUND, UNAVAILABLE, INVALID_RESPONSE }

internal class RecordReadException(val failure: RecordReadFailure) :
  Exception("record_read_${failure.name.lowercase()}")

// Only the fields needed by C17 are kept in memory. This is a read-only, one-record view.
internal class RecordPreview(
  val question: String,
  val decision: String,
  val winnerName: String,
)

internal sealed interface RecordReadResult {
  data object Empty : RecordReadResult
  class Found(val preview: RecordPreview) : RecordReadResult
}

internal class RecordsReadClient(private val engine: HttpClientEngine = OkHttp.create {
  config {
    followRedirects(false)
    followSslRedirects(false)
    retryOnConnectionFailure(false)
    cookieJar(CookieJar.NO_COOKIES)
    cache(null)
  }
}) : Closeable {
  private val client = HttpClient(engine) {
    expectSuccess = false
    followRedirects = false
    install(HttpTimeout) {
      connectTimeoutMillis = 10_000
      socketTimeoutMillis = 20_000
      requestTimeoutMillis = 20_000
    }
    install(ContentNegotiation) {
      json(Json { ignoreUnknownKeys = true })
    }
  }

  suspend fun firstRecord(accessToken: String, returnTo: String): RecordReadResult {
    if (!mobileOpaqueValue.matches(accessToken)) throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
    val requested = returnTo.removePrefix("/records/")
    val recordId = if (returnTo == "/") {
      val page: RecordPage = read("/api/v1/records?limit=1&sort=newest", accessToken)
      if (page.schemaVersion != 1 || page.items.size > 1 ||
        page.items.any { !mobileOpaqueValue.matches(it.recordId) }) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      page.items.firstOrNull()?.recordId ?: return RecordReadResult.Empty
    } else {
      if (!returnTo.startsWith("/records/") || !mobileOpaqueValue.matches(requested)) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      requested
    }
    val detail: RecordDetail = read("/api/v1/records/$recordId", accessToken)
    if (detail.schemaVersion != 2 || detail.recordId != recordId ||
      detail.question.isBlank() || detail.finalDecision.decision.isBlank() ||
      detail.result.winner != detail.finalDecision.winner ||
      detail.participants.size != 3 ||
      detail.participants.map { it.slot }.toSet() != PARTICIPANTS) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val winner = detail.participants.first { it.slot == detail.result.winner }
    if (winner.displayName.isBlank()) throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    return RecordReadResult.Found(
      RecordPreview(detail.question, detail.finalDecision.decision, winner.displayName))
  }

  private suspend inline fun <reified T> read(path: String, accessToken: String): T {
    try {
      val response = client.get("$RECORDS_ORIGIN$path") {
        bearerAuth(accessToken)
        header(HttpHeaders.CacheControl, "no-store")
        accept(ContentType.Application.Json)
      }
      when (response.status.value) {
        200 -> Unit
        401 -> throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
        404 -> throw RecordReadException(RecordReadFailure.NOT_FOUND)
        in 500..599 -> throw RecordReadException(RecordReadFailure.UNAVAILABLE)
        else -> throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      if (response.contentType()?.withoutParameters() != ContentType.Application.Json) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      return response.body()
    } catch (error: CancellationException) {
      throw error
    } catch (error: RecordReadException) {
      throw error
    } catch (_: IOException) {
      throw RecordReadException(RecordReadFailure.UNAVAILABLE)
    } catch (_: Exception) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
  }

  override fun close() {
    client.close()
    engine.close()
  }

  @Serializable private class RecordPage(val schemaVersion: Int, val items: List<RecordReference>)
  @Serializable private class RecordReference(val recordId: String)
  @Serializable private class RecordDetail(
    val schemaVersion: Int,
    val recordId: String,
    val question: String,
    val participants: List<RecordParticipant>,
    val result: RecordResult,
    val finalDecision: FinalDecision,
  )
  @Serializable private class RecordParticipant(val slot: String, val displayName: String)
  @Serializable private class RecordResult(val winner: String)
  @Serializable private class FinalDecision(val winner: String, val decision: String)

  private companion object {
    val PARTICIPANTS = setOf("participant-a", "participant-b", "participant-c")
  }
}
