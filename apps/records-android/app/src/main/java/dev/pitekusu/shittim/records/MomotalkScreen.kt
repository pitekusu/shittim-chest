package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import coil3.compose.AsyncImage
import coil3.request.CachePolicy
import coil3.request.ImageRequest
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimProgress
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimTheme
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.launch
import me.saket.telephoto.zoomable.ZoomableContentLocation
import me.saket.telephoto.zoomable.rememberZoomableState
import me.saket.telephoto.zoomable.zoomable

private val momotalkDate = DateTimeFormatter.ofPattern("M月d日", Locale.JAPAN)
private val momotalkFullDate = DateTimeFormatter.ofPattern("yyyy年M月d日", Locale.JAPAN)
private val momotalkZone = ZoneId.of("Asia/Tokyo")

@Composable
internal fun MomotalkScreen(state: MomotalkUiState, onEvent: (MomotalkEvent) -> Unit,
  modifier: Modifier = Modifier, showAppBar: Boolean = true) {
  Surface(modifier.fillMaxSize().testTag("momotalk-screen"), color = MaterialTheme.colorScheme.background,
    contentColor = MaterialTheme.colorScheme.onBackground) {
    Column(Modifier.fillMaxSize()) {
      if (showAppBar) MomotalkAppBar(state, onEvent)
      BoxWithConstraints(Modifier.weight(1f).fillMaxWidth()) {
        val twoPane = maxWidth >= 840.dp && LocalDensity.current.fontScale < 1.5f && state.selectedRoom != null
        if (twoPane) {
          Row(Modifier.fillMaxSize()) {
            MomotalkList(state, onEvent, Modifier.width(300.dp), roomPane = true)
            MomotalkList(state, onEvent, Modifier.weight(1f))
          }
        } else {
          Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
            MomotalkList(state, onEvent, Modifier.widthIn(max = 840.dp).fillMaxWidth())
          }
        }
      }
    }
  }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun MomotalkAppBar(state: MomotalkUiState, onEvent: (MomotalkEvent) -> Unit) {
  TopAppBar(title = { Text(stringResource(R.string.momotalk_title),
    style = MaterialTheme.typography.titleMediumEmphasized) },
    expandedHeight = if (LocalDensity.current.fontScale >= 1.5f) 112.dp else 64.dp,
    navigationIcon = {
      IconButton(onClick = { onEvent(MomotalkEvent.Back) }, Modifier.testTag("momotalk-back")) {
        Icon(painterResource(R.drawable.ic_arrow_back), stringResource(R.string.momotalk_back))
      }
    }, actions = {
      IconButton(onClick = { onEvent(MomotalkEvent.Refresh) },
        enabled = state.online && !state.activeResource().loading,
        modifier = Modifier.testTag("momotalk-refresh")) {
        Icon(painterResource(R.drawable.ic_refresh), stringResource(R.string.momotalk_refresh))
      }
    })
}

private fun MomotalkUiState.activeResource(): MomotalkResource<*> = when {
  selectedRoom != null -> detail
  selectedWeek != null -> rooms
  else -> weeks
}

@Composable
private fun MomotalkList(state: MomotalkUiState, onEvent: (MomotalkEvent) -> Unit,
  modifier: Modifier, roomPane: Boolean = false) {
  // Conversation text, names, image keys and API cursors never enter saved UI state.
  // A new room starts at its heading; refreshing the same room keeps the reading position.
  androidx.compose.runtime.key(if (roomPane) "rooms" else state.selectedRoom ?: state.selectedWeek ?: "weeks") {
    LazyColumn(modifier.fillMaxSize().testTag(if (roomPane) "momotalk-rooms-pane" else "momotalk-content"),
      contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
      if (roomPane || state.selectedRoom == null) {
        if (state.selectedWeek == null) weeksContent(state, onEvent) else roomsContent(state, onEvent, roomPane)
      } else conversationContent(state, onEvent)
    }
  }
}

private fun LazyListScope.weeksContent(state: MomotalkUiState, onEvent: (MomotalkEvent) -> Unit) {
  item {
    MomotalkHeading(stringResource(R.string.momotalk_weeks_title),
      stringResource(R.string.momotalk_weeks_description), "WEEKLY MOMOTALK")
  }
  resourceStatus(state.weeks, state.online, onEvent)
  val pages = state.weeks.data
  val weeks = pages.orEmpty().flatMap { it.weeks }.distinctBy { it.weekId }
  if (pages != null && weeks.isEmpty() && !state.weeks.loading) item { Text(stringResource(R.string.momotalk_empty_weeks)) }
  items(weeks, key = { it.weekId }) { week ->
    Surface(onClick = { onEvent(MomotalkEvent.SelectWeek(week.weekId)) },
      shape = MaterialTheme.shapes.large, color = MaterialTheme.colorScheme.surfaceContainer,
      modifier = Modifier.fillMaxWidth().testTag("momotalk-week-${week.weekId}")) {
      Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(stringResource(R.string.momotalk_week_title, formatMomotalkWeek(week.weekId)),
          style = MaterialTheme.typography.titleMediumEmphasized)
        Text(formatMomotalkPeriod(week), style = MaterialTheme.typography.bodyMedium,
          color = MaterialTheme.colorScheme.onSurfaceVariant)
        Text(stringResource(R.string.momotalk_week_publish, formatMomotalkTimestamp(week.publishAt)),
          style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.primary)
      }
    }
  }
  if (pages?.lastOrNull()?.nextCursor != null) item {
    OutlinedButton(onClick = { onEvent(MomotalkEvent.MoreWeeks) },
      enabled = state.online && !state.weeks.loading && state.weeks.error != MomotalkFailure.CURSOR_INVALID,
      modifier = Modifier.fillMaxWidth().testTag("momotalk-more-weeks")) {
      Text(stringResource(R.string.momotalk_more_weeks))
    }
  }
}

private fun LazyListScope.roomsContent(state: MomotalkUiState, onEvent: (MomotalkEvent) -> Unit, roomPane: Boolean) {
  val pages = state.rooms.data
  val week = pages?.firstOrNull()?.week ?: state.weeks.data.orEmpty().flatMap { it.weeks }
    .firstOrNull { it.weekId == state.selectedWeek }
  item {
    MomotalkHeading(stringResource(R.string.momotalk_rooms_title),
      if (week != null) formatMomotalkPeriod(week) else formatMomotalkWeek(state.selectedWeek.orEmpty()))
    MomotalkWeekPicker(state, onEvent)
    if (!roomPane) Text(stringResource(R.string.momotalk_rooms_description),
      Modifier.padding(top = 12.dp), style = MaterialTheme.typography.bodyMedium,
      color = MaterialTheme.colorScheme.onSurfaceVariant)
  }
  resourceStatus(state.rooms, state.online, onEvent, allowRefresh = !roomPane)
  val rooms = pages.orEmpty().flatMap { it.rooms }.distinctBy { it.roomId }
  if (pages != null && rooms.isEmpty() && !state.rooms.loading) item { Text(stringResource(R.string.momotalk_empty_rooms)) }
  items(rooms, key = { it.roomId }) { room ->
    MomotalkRoomCard(room, selected = room.roomId == state.selectedRoom, onClick = {
      onEvent(MomotalkEvent.SelectRoom(room.roomId))
    })
  }
  if (pages?.lastOrNull()?.nextCursor != null) item {
    OutlinedButton(onClick = { onEvent(MomotalkEvent.MoreRooms) },
      enabled = state.online && !state.rooms.loading && state.rooms.error != MomotalkFailure.CURSOR_INVALID,
      modifier = Modifier.fillMaxWidth().testTag("momotalk-more-rooms")) {
      Text(stringResource(R.string.momotalk_more_rooms))
    }
  }
}

@Composable
private fun MomotalkRoomCard(room: MomotalkRoomSummary, selected: Boolean, onClick: () -> Unit) {
  Surface(onClick = onClick, enabled = room.state == "ready", shape = MaterialTheme.shapes.large,
    color = if (selected) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceContainer,
    modifier = Modifier.fillMaxWidth().testTag("momotalk-room-${room.roomId}").semantics { this.selected = selected }) {
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
      Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
        // Avatars are restored by the authorized repository; Coil must never fetch a URL here.
        RequesterAvatar(room.requester.displayName, RecordAvatar(null, room.requester.avatar.fallbackVariant,
          room.requester.avatar.bytes), size = 48.dp)
        Text(room.requester.displayName, Modifier.weight(1f), style = MaterialTheme.typography.titleMediumEmphasized)
      }
      Text(stringResource(R.string.momotalk_question_count, room.questionCount), style = MaterialTheme.typography.bodyMedium,
        color = MaterialTheme.colorScheme.onSurfaceVariant)
      Text(stringResource(room.stateTitle()), style = MaterialTheme.typography.labelLarge,
        color = if (room.state == "ready") MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
    }
  }
}

@Composable
private fun MomotalkWeekPicker(state: MomotalkUiState, onEvent: (MomotalkEvent) -> Unit) {
  var expanded by remember { mutableStateOf(false) }
  val weeks = state.weeks.data.orEmpty().flatMap { it.weeks }.distinctBy { it.weekId }
  Box(Modifier.padding(top = 12.dp)) {
    OutlinedButton(onClick = { expanded = true }, modifier = Modifier.testTag("momotalk-week-picker")) {
      Text(stringResource(R.string.momotalk_week_title, formatMomotalkWeek(state.selectedWeek.orEmpty())))
    }
    DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
      weeks.forEach { week ->
        DropdownMenuItem(text = { Text(stringResource(R.string.momotalk_week_title, formatMomotalkWeek(week.weekId))) },
          onClick = { expanded = false; onEvent(MomotalkEvent.SelectWeek(week.weekId)) },
          modifier = Modifier.testTag("momotalk-week-choice-${week.weekId}").semantics {
            selected = week.weekId == state.selectedWeek
          })
      }
      if (state.weeks.data?.lastOrNull()?.nextCursor != null) DropdownMenuItem(
        text = { Text(stringResource(R.string.momotalk_more_weeks)) },
        onClick = { onEvent(MomotalkEvent.MoreWeeks) },
        enabled = state.online && !state.weeks.loading && state.weeks.error != MomotalkFailure.CURSOR_INVALID,
        modifier = Modifier.testTag("momotalk-more-weeks"))
      if (state.weeks.loading) DropdownMenuItem(text = { Text(stringResource(R.string.momotalk_loading)) },
        onClick = {}, enabled = false)
    }
  }
}

private fun MomotalkRoomSummary.stateTitle(): Int = when (state) {
  "ready" -> R.string.momotalk_ready
  "preparing" -> R.string.momotalk_preparing
  else -> R.string.momotalk_failed
}

private fun LazyListScope.conversationContent(state: MomotalkUiState, onEvent: (MomotalkEvent) -> Unit) {
  val detail = state.detail.data
  item {
    if (detail != null) {
      MomotalkHeading(stringResource(R.string.momotalk_conversation_title, detail.room.requester.displayName),
        formatMomotalkPeriod(detail.week), "MOMOTALK")
      Text(stringResource(R.string.momotalk_question_count, detail.room.questionCount),
        Modifier.padding(top = 12.dp), style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.onSurfaceVariant)
    } else MomotalkHeading(stringResource(R.string.momotalk_messages_title),
      stringResource(R.string.momotalk_conversation_description), "MOMOTALK")
  }
  resourceStatus(state.detail, state.online, onEvent)
  if (detail == null) return
  if (detail.room.state != "ready") item { Text(stringResource(detail.room.stateTitle())) }
  if (detail.messages.isEmpty() && detail.room.state == "ready") item { Text(stringResource(R.string.momotalk_empty_messages)) }
  items(detail.messages, key = { "message-${it.id}" }) { message ->
    val participant = detail.participants.firstOrNull { it.slot == message.participant }
    if (participant != null) MomotalkBubble(message, participant)
  }
  if (detail.images.isNotEmpty()) {
    item { ShittimSectionHeading(stringResource(R.string.momotalk_selfies_title), Modifier.padding(top = 8.dp)) }
    items(detail.images, key = { "image-${it.participant}-${it.mood}" }) { image ->
      MomotalkSelfieCard(image, detail, state.online, state.detail.loading,
        onRetry = { onEvent(MomotalkEvent.Refresh) })
    }
  }
}

@Composable
private fun MomotalkHeading(title: String, subtitle: String, kicker: String? = null) {
  Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
    ShittimSectionHeading(title, kicker = kicker, style = MaterialTheme.typography.headlineSmallEmphasized)
    Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
  }
}

private fun LazyListScope.resourceStatus(resource: MomotalkResource<*>, online: Boolean,
  onEvent: (MomotalkEvent) -> Unit, allowRefresh: Boolean = true) {
  if (!online) item {
    MomotalkNotice(stringResource(if (resource.data == null) R.string.momotalk_offline_missing else R.string.momotalk_offline),
      "momotalk-offline")
  }
  if (resource.saved) item { MomotalkNotice(stringResource(R.string.momotalk_saved), "momotalk-saved") }
  if (resource.loading) item {
    ShittimProgress(stringResource(R.string.momotalk_loading), Modifier.testTag("momotalk-loading"))
  }
  if (resource.error != null) item {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp), modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite }) {
      Text(stringResource(when (resource.error) {
        MomotalkFailure.AUTH_REQUIRED -> R.string.momotalk_auth_required
        MomotalkFailure.NOT_FOUND -> R.string.momotalk_not_found
        MomotalkFailure.STORAGE_UNAVAILABLE -> R.string.momotalk_storage_unavailable
        MomotalkFailure.CURSOR_INVALID -> R.string.momotalk_cursor_invalid
        else -> R.string.momotalk_unavailable
      }), color = MaterialTheme.colorScheme.onSurfaceVariant)
      if (online && allowRefresh && resource.error != MomotalkFailure.AUTH_REQUIRED && resource.error != MomotalkFailure.NOT_FOUND) {
        Button(onClick = { onEvent(MomotalkEvent.Refresh) }, enabled = !resource.loading,
          modifier = Modifier.testTag("momotalk-retry")) { Text(stringResource(R.string.momotalk_reload)) }
      }
    }
  }
}

@Composable
private fun MomotalkNotice(text: String, tag: String) {
  Surface(shape = MaterialTheme.shapes.medium, color = MaterialTheme.colorScheme.surfaceContainerHigh,
    modifier = Modifier.fillMaxWidth().testTag(tag)) {
    Text(text, Modifier.padding(12.dp).semantics { liveRegion = LiveRegionMode.Polite },
      style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
  }
}

@Composable
private fun MomotalkBubble(message: MomotalkMessage, participant: MomotalkParticipant) {
  val largeText = LocalDensity.current.fontScale >= 1.5f
  Column(Modifier.fillMaxWidth().testTag("momotalk-message-${message.id}").semantics(mergeDescendants = true) {},
    verticalArrangement = Arrangement.spacedBy(8.dp)) {
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
      ShittimParticipantAvatar(participant.displayName, participant.slot, size = if (largeText) 32.dp else 36.dp)
      Text(participant.displayName, Modifier.weight(1f), style = MaterialTheme.typography.labelLarge,
        color = shittimParticipantColor(participant.displayName, participant.slot))
    }
    val color = when (participant.slot) {
      "participant-b" -> MaterialTheme.colorScheme.secondaryContainer
      "participant-c" -> MaterialTheme.colorScheme.tertiaryContainer
      else -> MaterialTheme.colorScheme.primaryContainer
    }
    Surface(shape = RoundedCornerShape(topStart = 4.dp, topEnd = 20.dp, bottomStart = 20.dp, bottomEnd = 20.dp),
      color = color, modifier = Modifier.fillMaxWidth().padding(start = if (largeText) 0.dp else 44.dp)) {
      Text(message.text, Modifier.padding(16.dp), style = MaterialTheme.typography.bodyLarge,
        color = MaterialTheme.colorScheme.onSurface)
    }
  }
}

@Composable
private fun MomotalkSelfieCard(image: MomotalkImage, detail: MomotalkRoomDetail, online: Boolean,
  loading: Boolean, onRetry: () -> Unit) {
  var open by remember(detail.week.weekId, detail.room.roomId, image.participant, image.mood) { mutableStateOf(false) }
  val index = detail.images.indexOf(image)
  val people = image.subjects.ifEmpty { listOf(MomotalkImageSubject(image.participant, image.mood)) }
  val names = people.mapNotNull { subject -> detail.participants.firstOrNull { it.slot == subject.participant }?.displayName }
    .joinToString("・")
  Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
    Text(names, style = MaterialTheme.typography.titleSmallEmphasized)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
      people.forEach { subject ->
        val name = detail.participants.firstOrNull { it.slot == subject.participant }?.displayName ?: ""
        Text("$name：" + stringResource(if (subject.mood == "happy") R.string.momotalk_mood_happy else R.string.momotalk_mood_unhappy),
          style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
      }
    }
    val openDescription = stringResource(R.string.momotalk_selfie_open, index + 1)
    Surface(onClick = { open = true }, enabled = image.state == "ready", shape = MaterialTheme.shapes.large,
      color = MaterialTheme.colorScheme.surfaceContainerHigh,
      modifier = Modifier.fillMaxWidth().testTag("momotalk-selfie-$index")
        .semantics { contentDescription = openDescription }) {
      Box(Modifier.fillMaxWidth().height(240.dp), contentAlignment = Alignment.Center) {
        MomotalkSelfieContent(image, names, online, loading, onRetry, zoomable = false)
      }
    }
  }
  if (open) MomotalkSelfieViewer(detail, index, online, loading, onClose = { open = false }, onRetry = onRetry)
}

@Composable
private fun MomotalkSelfieViewer(detail: MomotalkRoomDetail, initial: Int, online: Boolean, loading: Boolean,
  onClose: () -> Unit, onRetry: () -> Unit) {
  val images = detail.images
  if (images.isEmpty()) return
  val pager = rememberPagerState(initialPage = initial.coerceIn(images.indices), pageCount = { images.size })
  val selected = pager.currentPage.coerceIn(images.indices)
  val scope = rememberCoroutineScope()
  Dialog(onDismissRequest = onClose, properties = DialogProperties(usePlatformDefaultWidth = false, decorFitsSystemWindows = false)) {
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surface) {
      Column(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
          Text(stringResource(R.string.momotalk_selfie_position, selected + 1, images.size), Modifier.weight(1f),
            style = MaterialTheme.typography.titleSmallEmphasized)
          TextButton(onClick = onClose, modifier = Modifier.testTag("momotalk-selfie-close")) {
            Text(stringResource(R.string.momotalk_selfie_close))
          }
        }
        HorizontalDivider()
        HorizontalPager(pager, Modifier.weight(1f).fillMaxWidth().testTag("momotalk-selfie-viewer"),
          key = { "${images[it].participant}-${images[it].mood}" }) { page ->
          val image = images[page]
          val people = image.subjects.ifEmpty { listOf(MomotalkImageSubject(image.participant, image.mood)) }
          val names = people.mapNotNull { subject -> detail.participants.firstOrNull { it.slot == subject.participant }?.displayName }
            .joinToString("・")
          Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            MomotalkSelfieContent(image, names, online, loading, onRetry, zoomable = true)
          }
        }
        if (images.size > 1) FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
          TextButton(onClick = { scope.launch { pager.animateScrollToPage(selected - 1) } }, enabled = selected > 0,
            modifier = Modifier.testTag("momotalk-selfie-previous")) { Text(stringResource(R.string.momotalk_selfie_previous)) }
          TextButton(onClick = { scope.launch { pager.animateScrollToPage(selected + 1) } }, enabled = selected < images.lastIndex,
            modifier = Modifier.testTag("momotalk-selfie-next")) { Text(stringResource(R.string.momotalk_selfie_next)) }
        }
      }
    }
  }
}

@Composable
private fun MomotalkSelfieContent(image: MomotalkImage, names: String, online: Boolean, loading: Boolean,
  onRetry: () -> Unit, zoomable: Boolean) {
  val bytes = image.bytes
  var failed by remember(bytes) { mutableStateOf(false) }
  var attempt by remember(bytes) { mutableIntStateOf(0) }
  if (bytes != null && !failed) {
    val context = LocalContext.current
    val request = remember(bytes, zoomable, attempt) {
      ImageRequest.Builder(context).data(bytes).size(if (zoomable) 4096 else 640)
        .memoryCachePolicy(CachePolicy.DISABLED).diskCachePolicy(CachePolicy.DISABLED).build()
    }
    var contentSize by remember(bytes) { mutableStateOf(Size(1f, 1f)) }
    val zoom = rememberZoomableState().apply {
      setContentLocation(ZoomableContentLocation.scaledInsideAndCenterAligned(contentSize))
    }
    AsyncImage(request, contentDescription = stringResource(R.string.momotalk_selfie_description, names),
      modifier = Modifier.fillMaxSize().then(if (zoomable) Modifier.zoomable(zoom) else Modifier),
      contentScale = if (zoomable) ContentScale.Inside else ContentScale.Fit,
      onSuccess = { success -> contentSize = Size(success.result.image.width.toFloat(), success.result.image.height.toFloat()) },
      onError = { failed = true })
  } else if (loading && image.state == "ready") {
    CircularProgressIndicator(Modifier.size(32.dp))
  } else {
    Column(Modifier.padding(16.dp), horizontalAlignment = Alignment.CenterHorizontally,
      verticalArrangement = Arrangement.spacedBy(12.dp)) {
      Icon(painterResource(R.drawable.ic_image), contentDescription = null, Modifier.size(32.dp),
        tint = MaterialTheme.colorScheme.onSurfaceVariant)
      Text(stringResource(when {
        image.state == "pending" -> R.string.momotalk_selfie_pending
        image.state == "failed" -> R.string.momotalk_selfie_failed
        !online -> R.string.momotalk_selfie_offline
        else -> R.string.momotalk_selfie_unavailable
      }), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
      if (online && image.state == "ready") TextButton(onClick = {
        if (failed) { failed = false; attempt++ } else onRetry()
      }) { Text(stringResource(R.string.momotalk_selfie_retry)) }
    }
  }
}

internal fun formatMomotalkWeek(value: String): String = runCatching { momotalkFullDate.format(LocalDate.parse(value)) }.getOrDefault(value)
private fun formatMomotalkTimestamp(value: String): String = runCatching {
  momotalkFullDate.format(Instant.parse(value).atZone(momotalkZone))
}.getOrDefault(value)
@Composable
private fun formatMomotalkPeriod(week: MomotalkWeek): String = stringResource(R.string.momotalk_week_period,
  runCatching { momotalkDate.format(Instant.parse(week.periodStart).atZone(momotalkZone)) }.getOrDefault(week.periodStart),
  // API periods are half-open; the final date belongs to the day before periodEnd.
  runCatching { momotalkDate.format(Instant.parse(week.periodEnd).minusNanos(1).atZone(momotalkZone)) }.getOrDefault(week.periodEnd))

@Preview(name = "Momotalk weeks", widthDp = 360)
@Composable private fun MomotalkWeeksPreview() { ShittimTheme { MomotalkScreen(momotalkSampleState(), {}) } }
@Preview(name = "Momotalk conversation", widthDp = 360)
@Composable private fun MomotalkConversationPreview() { ShittimTheme { MomotalkScreen(momotalkSampleState(openRoom = true), {}) } }
@Preview(name = "Momotalk large text", widthDp = 320, fontScale = 2f)
@Composable private fun MomotalkLargeTextPreview() { ShittimTheme(true) { MomotalkScreen(momotalkSampleState(openRoom = true), {}) } }
@Preview(name = "Momotalk tablet", widthDp = 1000)
@Composable private fun MomotalkTabletPreview() { ShittimTheme { MomotalkScreen(momotalkSampleState(openRoom = true), {}) } }
