package dev.pitekusu.shittim.records

import android.content.Context
import dev.pitekusu.shittim.records.auth.CacheAuthorization
import dev.pitekusu.shittim.records.storage.EncryptedRecordImageStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

internal fun createStudentGallery(
  context: Context,
  runtime: RecordsRuntime,
  owner: String,
  permit: CacheAuthorization?,
): StudentGallery? {
  val session = runtime.session ?: return null
  if (runtime.demo != null || permit == null) return null
  val appContext = context.applicationContext
  return object : StudentGallery {
    private val client = lazy { RecordsReadClient() }
    private val images by lazy { EncryptedRecordImageStore(appContext) }
    // The collection and duplicate grids can ask for the same image concurrently.
    private val thumbnailLoads = Mutex()
    @Volatile private var closed = false

    private fun active(): Boolean =
      !closed && runtime.isActive(owner) && session.cachePermit.value === permit

    private fun requireActive() {
      if (!active()) throw GalleryReadException(GalleryReadFailure.AUTH_REQUIRED)
    }

    private suspend fun <T> authorized(read: suspend (RecordsReadClient, String) -> T): T {
      requireActive()
      if (!hasValidatedDebateNetwork(appContext)) throw GalleryReadException(GalleryReadFailure.UNAVAILABLE)
      val result = try {
        session.withAuthorizedToken { token ->
          requireActive()
          read(client.value, token)
        }
      } catch (error: GalleryReadException) {
        if (error.serverRejectedSession && active()) session.onAuthenticationRequired()
        throw error
      }
      requireActive()
      return result ?: throw GalleryReadException(GalleryReadFailure.UNAVAILABLE)
    }

    override suspend fun loadCollection(): StudentCollection = authorized { api, token ->
      api.galleryCollection(token, ::requireActive)
    }

    override suspend fun loadThumbnails(ids: List<String>): Map<String, ByteArray> = thumbnailLoads.withLock {
      if (ids.size !in 1..GALLERY_THUMBNAIL_BATCH_SIZE || ids.toSet().size != ids.size ||
        ids.any { galleryDrawId(it) == null }) {
        throw GalleryReadException(GalleryReadFailure.INVALID_RESPONSE)
      }
      val cached = cachedImages(ids, thumbnail = true)
      try {
        val missing = ids.filterNot(cached::containsKey)
        if (missing.isEmpty() || !hasValidatedDebateNetwork(appContext)) return@withLock cached

        val downloaded = try {
          authorized { api, token ->
            // Limit concurrent image downloads and preserve successes when a signed URL fails.
            api.galleryThumbnails(token, missing).chunked(4).flatMap { batch ->
              coroutineScope {
                batch.map { ticket ->
                  async {
                    requireActive()
                    try {
                      ticket.cardId to api.galleryImage(ticket.url, ticket.expiresAt, ticket.cardId, thumbnail = true)
                    } catch (_: GalleryReadException) { null }
                  }
                }.awaitAll().filterNotNull()
              }
            }.toMap()
          }
        } catch (error: GalleryReadException) {
          if (error.failure != GalleryReadFailure.UNAVAILABLE || cached.isEmpty()) throw error
          emptyMap()
        }
        saveImages(downloaded, thumbnail = true)
        cached + downloaded
      } catch (error: Exception) {
        cached.values.forEach { it.fill(0) }
        throw error
      }
    }

    override suspend fun loadOriginal(id: String): ByteArray {
      if (galleryDrawId(id) == null) throw GalleryReadException(GalleryReadFailure.INVALID_RESPONSE)
      cachedImages(listOf(id), thumbnail = false)[id]?.let { return it }
      val bytes = authorized { api, token ->
        val ticket = api.galleryOriginal(token, id)
        requireActive()
        api.galleryImage(ticket.url, ticket.expiresAt, id, thumbnail = false)
      }
      saveImages(mapOf(id to bytes), thumbnail = false)
      return bytes
    }

    private suspend fun cachedImages(ids: List<String>, thumbnail: Boolean): Map<String, ByteArray> =
      RecordCacheAccount.lock.withLock {
        val cached = mutableMapOf<String, ByteArray>()
        try {
          ids.forEach { id ->
            requireActive()
            val bytes = try { images.readGallery(owner, id, thumbnail) }
            catch (_: RecordImageException) { null }
            if (bytes != null) cached[id] = bytes
          }
          requireActive()
          cached
        } catch (error: Exception) {
          cached.values.forEach { it.fill(0) }
          throw error
        }
      }

    private suspend fun saveImages(downloaded: Map<String, ByteArray>, thumbnail: Boolean) {
      try {
        if (downloaded.isNotEmpty()) RecordCacheAccount.lock.withLock {
          downloaded.forEach { (id, bytes) ->
            requireActive()
            try { images.saveGallery(owner, id, thumbnail, bytes) }
            catch (_: RecordImageException) { /* An image can still be displayed when storage is unavailable. */ }
          }
        }
        requireActive()
      } catch (error: Exception) {
        downloaded.values.forEach { it.fill(0) }
        throw error
      }
    }

    override fun close() {
      closed = true
      if (client.isInitialized()) client.value.close()
    }
  }
}
