package dev.pitekusu.shittim.records

import androidx.annotation.StringRes
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyGridState
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.input.TextFieldLineLimits
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.clearText
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import coil3.compose.AsyncImage
import coil3.request.CachePolicy
import coil3.request.ImageRequest
import dev.pitekusu.shittim.records.ui.ShittimProgress
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.collectLatest

private const val THUMBNAIL_MEMORY_LIMIT = 48
private val galleryDate = DateTimeFormatter.ofPattern("yyyy/MM/dd", Locale.JAPAN).withZone(ZoneId.of("Asia/Tokyo"))

@Composable
internal fun StudentCollectionScreen(gallery: StudentGallery, isOnline: Boolean, modifier: Modifier = Modifier) {
  key(gallery) {
    var collection by remember { mutableStateOf<StudentCollection?>(null) }
    var refreshing by remember { mutableStateOf(false) }
    var failed by remember { mutableStateOf(false) }
    var attempt by remember { mutableIntStateOf(0) }
    LaunchedEffect(isOnline, attempt) {
      if (!isOnline) return@LaunchedEffect
      refreshing = true
      failed = false
      try {
        collection = gallery.loadCollection()
      } catch (_: GalleryReadException) {
        failed = true
      } finally {
        refreshing = false
      }
    }
    val data = collection
    Surface(modifier.fillMaxSize().testTag("student-collection-screen")) {
      if (data == null) {
        Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
          if (isOnline && !failed) {
            ShittimProgress(stringResource(R.string.gallery_loading), Modifier.widthIn(max = 360.dp))
          } else {
            GalleryStatus(if (isOnline) R.string.gallery_load_failed else R.string.gallery_offline,
              onRetry = if (isOnline) ({ attempt++ }) else null)
          }
        }
      } else {
        GalleryCollectionContent(data, gallery, isOnline, refreshing, failed, attempt) { attempt++ }
      }
    }
  }
}

private data class GalleryTile(
  val id: String,
  val name: String?,
  val school: String?,
  val rarity: Int?,
  val cover: GalleryCard?,
  val count: Int? = null,
  val first: Boolean = false,
)

private fun GalleryCard.tile(first: Boolean = false) =
  GalleryTile(cardId, studentName, schoolName, rarity, this, first = first)

@Composable
private fun GalleryCollectionContent(collection: StudentCollection, gallery: StudentGallery, isOnline: Boolean,
  refreshing: Boolean, failed: Boolean, attempt: Int, onRefresh: () -> Unit) {
  var cardsView by remember { mutableStateOf(false) }
  var collectionFilters by remember { mutableStateOf(GalleryFilters()) }
  var cardFilters by remember { mutableStateOf(GalleryFilters(sort = GallerySort.Newest)) }
  // Keep each tab's search text in memory, outside SavedState.
  val collectionSearch = remember { TextFieldState() }
  val cardSearch = remember { TextFieldState() }
  var groupId by remember { mutableStateOf<String?>(null) }
  var cardId by remember { mutableStateOf<String?>(null) }
  val search = if (cardsView) cardSearch else collectionSearch
  val options = if (cardsView) cardFilters else collectionFilters
  val filters = options.copy(query = search.text.toString())
  val updateFilters: (GalleryFilters) -> Unit = {
    if (cardsView) cardFilters = it else collectionFilters = it
  }
  val gridState = rememberLazyGridState()
  val tiles = remember(collection, cardsView, filters) {
    if (cardsView) filters.cards(collection).map { it.tile() }
    else filters.entries(collection).map {
      GalleryTile(it.studentId, it.studentName, it.schoolName, it.rarity, it.cards.firstOrNull(), it.cards.size)
    }
  }
  val acquired = remember(collection) { collection.entries.count { it.cards.isNotEmpty() } }
  val schools = remember(collection, cardsView) { gallerySchoolOptions(collection, cardsView) }
  val focus = LocalFocusManager.current
  LaunchedEffect(cardsView) { gridState.scrollToItem(0) }

  GalleryGrid(tiles, gridState, gallery, isOnline, attempt, Modifier.fillMaxSize().testTag("gallery-grid"),
    onClick = { tile ->
      focus.clearFocus()
      if ((tile.count ?: 0) > 1) groupId = tile.id else cardId = tile.cover?.cardId
    }, header = {
      Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        if (refreshing) LinearProgressIndicator(Modifier.fillMaxWidth())
        if (!isOnline || failed) Text(stringResource(if (isOnline) R.string.gallery_load_failed else R.string.gallery_offline),
          color = if (failed && isOnline) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
          style = MaterialTheme.typography.bodyMedium, modifier = Modifier.semantics {
            liveRegion = LiveRegionMode.Polite
          })
        FlowRow(Modifier.fillMaxWidth().selectableGroup(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
          FilterChip(selected = !cardsView, onClick = { cardsView = false },
            label = { Text(stringResource(R.string.gallery_collection_tab)) }, modifier = Modifier.testTag("gallery-collection-tab"))
          FilterChip(selected = cardsView, onClick = { cardsView = true },
            label = { Text(stringResource(R.string.gallery_cards_tab)) }, modifier = Modifier.testTag("gallery-cards-tab"))
        }
        GalleryFilterControls(options, search, cardsView, schools, updateFilters)
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
          val count = if (cardsView) stringResource(R.string.gallery_cards_count, collection.cards.size)
            else stringResource(R.string.gallery_collection_count, acquired, collection.entries.size)
          Column(Modifier.weight(1f).padding(top = 8.dp)) {
            Text(count, Modifier.semantics { liveRegion = LiveRegionMode.Polite },
              style = MaterialTheme.typography.titleMediumEmphasized)
            if (filters.hasFilters) TextButton(onClick = {
              focus.clearFocus()
              search.clearText()
              updateFilters(options.clearFilters())
            }, modifier = Modifier.testTag("gallery-clear-filters")) {
              Text(stringResource(R.string.gallery_clear_filters))
            }
          }
          IconButton(onClick = onRefresh, enabled = isOnline && !refreshing,
            modifier = Modifier.testTag("gallery-refresh")) {
            Icon(painterResource(R.drawable.ic_refresh), stringResource(R.string.gallery_refresh))
          }
        }
        if (tiles.isEmpty()) Text(stringResource(if (cardsView) R.string.gallery_no_results else R.string.gallery_no_students),
          Modifier.padding(vertical = 16.dp), color = MaterialTheme.colorScheme.onSurfaceVariant)
      }
    })

  val group = collection.entries.firstOrNull { it.studentId == groupId && it.cards.isNotEmpty() }
  val viewerCards = group?.cards ?: tiles.mapNotNull { it.cover }
  val index = viewerCards.indexOfFirst { it.cardId == cardId }
  if (group != null || index >= 0) {
    val groupGrid = rememberLazyGridState()
    val onBack = { if (index >= 0) cardId = null else groupId = null }
    Dialog(onDismissRequest = onBack, properties = DialogProperties(usePlatformDefaultWidth = false,
      decorFitsSystemWindows = false, dismissOnClickOutside = false)) {
      Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surface) {
        Column(Modifier.fillMaxSize().safeDrawingPadding()) {
          if (index >= 0) {
            GalleryCardViewer(viewerCards, index, gallery, isOnline, onBack) { cardId = it }
          } else if (group != null) {
            GalleryDialogBar(group.studentName ?: stringResource(R.string.gallery_cards_tab),
              stringResource(R.string.gallery_back_to_collection), "gallery-group-back", onBack)
            val copies = remember(group) { group.cards.mapIndexed { copyIndex, card -> card.tile(first = copyIndex == 0) } }
            GalleryGrid(copies, groupGrid, gallery, isOnline, attempt,
              Modifier.fillMaxSize().testTag("gallery-group-grid"), onClick = { cardId = it.cover?.cardId })
          }
        }
      }
    }
  }
}

@Composable
private fun GalleryFilterControls(filters: GalleryFilters, searchState: TextFieldState, cardsView: Boolean,
  schools: List<String>, onChange: (GalleryFilters) -> Unit) {
  val focus = LocalFocusManager.current
  val all = stringResource(R.string.record_filter_all)
  val search: @Composable (Modifier) -> Unit = { modifier ->
    OutlinedTextField(state = searchState, modifier = modifier.testTag("gallery-search"),
      label = { Text(stringResource(R.string.gallery_student_name)) },
      placeholder = { Text(stringResource(R.string.gallery_search)) },
      lineLimits = TextFieldLineLimits.SingleLine,
      keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
      onKeyboardAction = { focus.clearFocus() },
      trailingIcon = if (searchState.text.isEmpty()) null else ({
        IconButton(onClick = { searchState.clearText() }) {
          Icon(painterResource(R.drawable.ic_close), stringResource(R.string.record_search_clear_text))
        }
      }))
  }
  val school: @Composable (Modifier) -> Unit = { modifier ->
    GalleryPicker(stringResource(R.string.gallery_school), filters.school, listOf(null) + schools,
      label = { Text(it ?: all) }, onPick = { onChange(filters.copy(school = it)) },
      modifier = modifier, tag = "gallery-school-filter")
  }
  val rarity: @Composable (Modifier) -> Unit = { modifier ->
    GalleryPicker(stringResource(R.string.gallery_rarity), filters.rarity, listOf(null) + GALLERY_RARITIES,
      label = { if (it == null) Text(all) else GalleryRarity(it) }, onPick = { onChange(filters.copy(rarity = it)) },
      modifier = modifier, tag = "gallery-rarity-filter")
  }
  val sort: @Composable (Modifier) -> Unit = { modifier ->
    GalleryPicker(stringResource(R.string.gallery_sort), filters.sort, GallerySort.entries,
      label = { Text(stringResource(it.label(cardsView))) }, onPick = { onChange(filters.copy(sort = it)) },
      modifier = modifier, tag = "gallery-sort")
  }
  val fontScale = LocalDensity.current.fontScale
  BoxWithConstraints(Modifier.fillMaxWidth()) {
    val width = maxWidth
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
      if (width >= 760.dp && fontScale < 1.3f) {
        Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.Bottom) {
          search(Modifier.weight(1.4f))
          school(Modifier.weight(1f))
          rarity(Modifier.weight(.75f))
          sort(Modifier.weight(1.5f))
        }
      } else {
        search(Modifier.fillMaxWidth())
        if (width >= 264.dp && fontScale < 1.5f) {
          Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.Bottom) {
            school(Modifier.weight(1f))
            rarity(Modifier.weight(1f))
          }
        } else {
          school(Modifier.fillMaxWidth())
          rarity(Modifier.fillMaxWidth())
        }
        sort(Modifier.fillMaxWidth())
      }
      if (!cardsView) Row(Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("gallery-acquired-only")
        .toggleable(filters.acquiredOnly, role = Role.Checkbox, onValueChange = { onChange(filters.copy(acquiredOnly = it)) }),
        verticalAlignment = Alignment.CenterVertically) {
        Checkbox(checked = filters.acquiredOnly, onCheckedChange = null, modifier = Modifier.padding(horizontal = 12.dp))
        Text(stringResource(R.string.gallery_acquired_only))
      }
    }
  }
}

@StringRes
private fun GallerySort.label(cardsView: Boolean): Int = when (this) {
  GallerySort.Common -> R.string.gallery_common
  GallerySort.Rare -> R.string.gallery_rare
  GallerySort.Newest -> if (cardsView) R.string.gallery_card_newest else R.string.gallery_first_newest
  GallerySort.Oldest -> if (cardsView) R.string.gallery_card_oldest else R.string.gallery_first_oldest
}

@Composable
private fun <T> GalleryPicker(title: String, value: T, choices: List<T>, label: @Composable (T) -> Unit,
  onPick: (T) -> Unit, modifier: Modifier, tag: String) {
  val focus = LocalFocusManager.current
  var expanded by remember { mutableStateOf(false) }
  Column(modifier, verticalArrangement = Arrangement.spacedBy(4.dp)) {
    Text(title, style = MaterialTheme.typography.labelLarge)
    Box {
      OutlinedButton(onClick = { focus.clearFocus(); expanded = true },
        modifier = Modifier.fillMaxWidth().testTag(tag).semantics { contentDescription = title },
        shape = MaterialTheme.shapes.medium, contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp)) {
        Box(Modifier.weight(1f)) { label(value) }
        Icon(painterResource(R.drawable.ic_expand_more), null, Modifier.size(24.dp))
      }
      DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false },
        Modifier.heightIn(max = 320.dp).testTag("$tag-menu")) {
        choices.forEach { choice ->
          DropdownMenuItem(text = { label(choice) }, onClick = { onPick(choice); expanded = false })
        }
      }
    }
  }
}

@Composable
private fun GalleryGrid(tiles: List<GalleryTile>, state: LazyGridState, gallery: StudentGallery, isOnline: Boolean,
  attempt: Int, modifier: Modifier, onClick: (GalleryTile) -> Unit, header: (@Composable () -> Unit)? = null) {
  val covers = remember(tiles) {
    tiles.mapNotNull { tile -> tile.cover?.takeIf { it.thumbnailReady }?.let { tile.id to it.cardId } }.toMap()
  }
  val thumbnails = remember(gallery, attempt) { mutableStateMapOf<String, ByteArray?>() }
  LaunchedEffect(state, gallery, covers, isOnline, attempt) {
    // Runtime reads the encrypted cache first and skips remote requests when offline.
    thumbnails.filterValues { it == null }.keys.forEach(thumbnails::remove)
    snapshotFlow { state.layoutInfo.visibleItemsInfo.mapNotNull { (it.key as? String)?.let(covers::get) }.distinct() }
      .collectLatest { visible ->
        delay(100)
        for (chunk in visible.filterNot(thumbnails::containsKey).chunked(GALLERY_THUMBNAIL_BATCH_SIZE)) {
          val result = try { gallery.loadThumbnails(chunk) } catch (_: GalleryReadException) { emptyMap() }
          for (id in chunk) thumbnails[id] = result[id]
        }
        // Keep only a small working set of private thumbnails in the current screen.
        thumbnails.keys.filterNot(visible::contains).take((thumbnails.size - THUMBNAIL_MEMORY_LIMIT).coerceAtLeast(0))
          .forEach(thumbnails::remove)
      }
  }
  val minimumWidth = if (LocalDensity.current.fontScale >= 1.5f) 200.dp else 148.dp
  LazyVerticalGrid(GridCells.Adaptive(minimumWidth), modifier, state = state,
    contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 4.dp, bottom = 24.dp),
    horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
    if (header != null) item(key = "gallery-controls", span = { GridItemSpan(maxLineSpan) }) { header() }
    items(tiles, key = { it.id }) { tile ->
      GalleryTileContent(tile, tile.cover?.let { thumbnails[it.cardId] }, onClick = { onClick(tile) })
    }
  }
}

@Composable
private fun GalleryTileContent(tile: GalleryTile, bytes: ByteArray?, onClick: () -> Unit) {
  val colors = MaterialTheme.colorScheme
  val title = tile.name ?: stringResource(R.string.gallery_secret)
  val cover = tile.cover
  val dialogTag = if (tile.count == null) "gallery-card" else "gallery-entry"
  val tileModifier = Modifier.fillMaxWidth().testTag("$dialogTag-${tile.id}")
  val shape = MaterialTheme.shapes.large
  val border = BorderStroke(1.dp, colors.outlineVariant)
  val content: @Composable () -> Unit = {
    Column {
      val secret = tile.name == null
      Surface(color = if (secret) colors.tertiaryContainer else colors.surfaceContainerHigh) {
        Box(Modifier.fillMaxWidth().aspectRatio(768f / 355f), contentAlignment = Alignment.Center) {
          if (cover == null) Text("?", Modifier.clearAndSetSemantics {},
            style = MaterialTheme.typography.headlineLargeEmphasized,
            color = if (secret) colors.onTertiaryContainer else colors.onSurfaceVariant)
          else {
            val image = rememberGalleryThumbnail(bytes)
            Icon(painterResource(R.drawable.ic_image), null, Modifier.size(28.dp), tint = colors.onSurfaceVariant)
            if (image != null) AsyncImage(image, null, Modifier.fillMaxSize(), contentScale = ContentScale.Fit)
          }
        }
      }
      Column(Modifier.fillMaxWidth().padding(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(title, style = MaterialTheme.typography.titleSmallEmphasized, maxLines = 2, overflow = TextOverflow.Ellipsis)
        tile.school?.let {
          Text(it, style = MaterialTheme.typography.bodySmall, color = colors.onSurfaceVariant,
            maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
          GalleryRarity(tile.rarity)
          val badge = when {
            cover == null -> stringResource(R.string.gallery_not_acquired)
            tile.count != null -> stringResource(R.string.gallery_copy_count, tile.count)
            tile.first -> stringResource(R.string.gallery_first_card)
            else -> null
          }
          if (badge != null) {
            Surface(shape = MaterialTheme.shapes.small,
              color = if (cover == null) colors.surfaceContainerHighest else colors.primaryContainer) {
              Text(badge, Modifier.padding(horizontal = 6.dp, vertical = 2.dp), style = MaterialTheme.typography.labelMedium)
            }
          }
        }
        if (tile.count == null && cover != null) {
          Text(galleryDate.format(cover.postedAt), style = MaterialTheme.typography.bodySmall, color = colors.onSurfaceVariant)
        }
      }
    }
  }
  if (cover == null) Surface(tileModifier, shape = shape, color = colors.surfaceContainerLow, border = border, content = content)
  else Surface(onClick, tileModifier, shape = shape, color = colors.surfaceContainerLow, border = border, content = content)
}

@StringRes
private fun galleryRarityResource(rarity: Int?) = when (rarity) {
  null -> R.string.gallery_rarity_unknown
  0 -> R.string.gallery_starless
  else -> R.string.gallery_stars
}

@Composable
private fun GalleryRarity(rarity: Int?) {
  val label = stringResource(galleryRarityResource(rarity), rarity ?: 0)
  Text(if (rarity != null && rarity in 1..4) "★".repeat(rarity) else label,
    Modifier.clearAndSetSemantics { contentDescription = label },
    style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun GalleryDialogBar(title: String, backLabel: String, testTag: String, onBack: () -> Unit) {
  TopAppBar(title = { Text(title, style = MaterialTheme.typography.titleMediumEmphasized, maxLines = 2,
    overflow = TextOverflow.Ellipsis) },
    expandedHeight = if (LocalDensity.current.fontScale >= 1.5f) 112.dp else 64.dp,
    navigationIcon = {
    IconButton(onClick = onBack, modifier = Modifier.testTag(testTag)) {
      Icon(painterResource(R.drawable.ic_arrow_back), backLabel)
    }
  })
}

@Composable
private fun GalleryCardViewer(cards: List<GalleryCard>, index: Int, gallery: StudentGallery,
  isOnline: Boolean, onBack: () -> Unit, onSelect: (String) -> Unit) {
  val card = cards[index]
  GalleryDialogBar(card.studentName, stringResource(R.string.gallery_close_card), "gallery-viewer-back", onBack)
  BoxWithConstraints(Modifier.fillMaxSize().testTag("gallery-viewer")) {
    val metadataHeight = maxHeight * .42f
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
      Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
        GalleryOriginal(gallery, card.cardId, isOnline)
      }
      HorizontalDivider()
      Column(Modifier.fillMaxWidth().heightIn(max = metadataHeight).verticalScroll(rememberScrollState())
        .padding(vertical = 4.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        FlowRow(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
          Text(card.schoolName, style = MaterialTheme.typography.labelLarge)
          GalleryRarity(card.rarity)
          Text(galleryDate.format(card.postedAt), style = MaterialTheme.typography.bodySmall)
        }
        val performer = when (card.performer) {
          "participant-a" -> R.string.gallery_performer_a
          "participant-b" -> R.string.gallery_performer_b
          else -> R.string.gallery_performer_c
        }
        Text(stringResource(R.string.gallery_performer, stringResource(performer)), style = MaterialTheme.typography.bodyMedium)
        val collector = card.collectorName ?: stringResource(if (card.isMine) R.string.gallery_mine else R.string.gallery_unknown)
        Text(stringResource(R.string.gallery_collector, collector), style = MaterialTheme.typography.bodyMedium)
      }
      if (cards.size > 1) {
        FlowRow(Modifier.fillMaxWidth().testTag("gallery-viewer-position"), horizontalArrangement = Arrangement.SpaceBetween,
          itemVerticalAlignment = Alignment.CenterVertically) {
          TextButton(onClick = { onSelect(cards[index - 1].cardId) }, enabled = index > 0,
            modifier = Modifier.testTag("gallery-viewer-previous")) { Text(stringResource(R.string.insights_previous)) }
          Text(stringResource(R.string.gallery_card_position, index + 1, cards.size),
            style = MaterialTheme.typography.labelLarge)
          TextButton(onClick = { onSelect(cards[index + 1].cardId) }, enabled = index < cards.lastIndex,
            modifier = Modifier.testTag("gallery-viewer-next")) { Text(stringResource(R.string.insights_next)) }
        }
      }
    }
  }
}

@Composable
private fun GalleryOriginal(gallery: StudentGallery, cardId: String, isOnline: Boolean) {
  var bytes by remember(cardId) { mutableStateOf<ByteArray?>(null) }
  var failed by remember(cardId) { mutableStateOf(false) }
  var notFound by remember(cardId) { mutableStateOf(false) }
  var attempt by remember(cardId) { mutableIntStateOf(0) }
  LaunchedEffect(cardId, isOnline, attempt) {
    if (bytes != null) return@LaunchedEffect
    failed = false
    notFound = false
    try {
      bytes = gallery.loadOriginal(cardId)
    } catch (problem: GalleryReadException) {
      failed = true
      notFound = problem.failure == GalleryReadFailure.NOT_FOUND
    }
  }
  val image = bytes
  if (image != null && !failed) {
    GalleryOriginalImage(image, onError = { failed = true })
  } else if (!failed) {
    ShittimProgress(stringResource(R.string.gallery_original_loading), Modifier.widthIn(max = 360.dp))
  } else {
    val message = when {
      !isOnline -> R.string.gallery_offline
      notFound -> R.string.gallery_original_missing
      else -> R.string.gallery_original_failed
    }
    GalleryStatus(message, onRetry = if (isOnline && !notFound) ({ bytes = null; attempt++ }) else null)
  }
}

@Composable
private fun rememberGalleryThumbnail(bytes: ByteArray?): ImageRequest? {
  val context = LocalContext.current
  return remember(context, bytes) {
    bytes?.let {
      ImageRequest.Builder(context).data(it).size(768)
        .memoryCachePolicy(CachePolicy.DISABLED).diskCachePolicy(CachePolicy.DISABLED).build()
    }
  }
}

@Composable
private fun GalleryStatus(@StringRes message: Int, onRetry: (() -> Unit)?) {
  Column(horizontalAlignment = Alignment.CenterHorizontally,
    verticalArrangement = Arrangement.spacedBy(8.dp)) {
    Text(stringResource(message), style = MaterialTheme.typography.bodyMedium,
      modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
    if (onRetry != null) TextButton(onClick = onRetry, modifier = Modifier.testTag("gallery-retry")) {
      Text(stringResource(R.string.session_retry))
    }
  }
}
