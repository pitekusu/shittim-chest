package dev.pitekusu.shittim.records

import android.content.Context
import dev.pitekusu.shittim.records.auth.CacheAuthorization

/** Reuse the app's token recovery, exact permit fence and encrypted account store. */
internal fun createMomotalkModel(context: Context, runtime: RecordsRuntime, owner: String,
  permit: CacheAuthorization?, initial: MomotalkSelection = MomotalkSelection()): MomotalkModel {
  val active = { runtime.isActive(owner) && (runtime.demo != null || runtime.session?.cachePermit?.value === permit) }
  if (runtime.demo != null) {
    val sample = momotalkSampleState()
    fun guard() { if (!active()) throw MomotalkException(MomotalkFailure.AUTH_REQUIRED) }
    return MomotalkModel(active,
      cachedWeeks = { guard(); sample.weeks.data.orEmpty() },
      refreshWeeks = { guard(); sample.weeks.data.orEmpty() },
      appendWeeks = { guard(); sample.weeks.data.orEmpty() },
      cachedRooms = { guard(); sample.rooms.data.orEmpty().filter { page -> page.week.weekId == it } },
      refreshRooms = { guard(); sample.rooms.data.orEmpty().filter { page -> page.week.weekId == it } },
      appendRooms = { week, _ -> guard(); sample.rooms.data.orEmpty().filter { it.week.weekId == week } },
      cachedRoom = { week, room -> guard(); sample.detail.data?.takeIf { it.week.weekId == week && it.room.roomId == room } },
      refreshRoom = { week, room -> guard(); requireNotNull(sample.detail.data?.takeIf { it.week.weekId == week && it.room.roomId == room }) },
      initial = initial)
  }
  val repository = lazy { MomotalkRepository.open(context.applicationContext) { account -> account == owner && active() } }
  suspend fun <T> authorized(read: suspend (MomotalkRepository, String) -> T): T {
    if (!active()) throw MomotalkException(MomotalkFailure.AUTH_REQUIRED)
    if (!hasValidatedDebateNetwork(context)) throw MomotalkException(MomotalkFailure.UNAVAILABLE)
    return runtime.session?.withAuthorizedToken { token ->
      if (!active()) throw MomotalkException(MomotalkFailure.AUTH_REQUIRED)
      read(repository.value, token)
    } ?: throw MomotalkException(if (active()) MomotalkFailure.UNAVAILABLE else MomotalkFailure.AUTH_REQUIRED)
  }
  return MomotalkModel(active,
    cachedWeeks = { repository.value.cachedWeeks(owner).map { it.value } },
    refreshWeeks = { authorized { repo, token -> repo.refreshWeeks(token, owner).map { it.value } } },
    appendWeeks = { cursor -> authorized { repo, token -> repo.appendWeeks(token, owner, cursor).map { it.value } } },
    cachedRooms = { repository.value.cachedRooms(owner, it).map { saved -> saved.value } },
    refreshRooms = { week -> authorized { repo, token -> repo.refreshRooms(token, owner, week).map { it.value } } },
    appendRooms = { week, cursor -> authorized { repo, token -> repo.appendRooms(token, owner, week, cursor).map { it.value } } },
    cachedRoom = { week, room -> repository.value.cachedRoom(owner, week, room)?.value },
    refreshRoom = { week, room -> authorized { repo, token -> repo.refreshRoom(token, owner, week, room).value } },
    warmRooms = { week -> repository.value.warmRoomsAvatars(owner, week); repository.value.cachedRooms(owner, week).map { it.value } },
    warmRoom = { week, room -> repository.value.warmRoomImages(owner, week, room)
      repository.value.warmRoomAvatars(owner, week, room)
      repository.value.cachedRoom(owner, week, room)?.value },
    authenticationRequired = { if (active()) runtime.session?.onAuthenticationRequired() },
    close = { if (repository.isInitialized()) repository.value.close() }, initial = initial)
}
