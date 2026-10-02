package dev.pitekusu.shittim.records

import androidx.annotation.DrawableRes
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.input.TextFieldLineLimits
import androidx.compose.foundation.text.input.TextFieldState
import androidx.compose.foundation.text.input.clearText
import androidx.compose.foundation.text.input.setTextAndPlaceCursorAtEnd
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExpandedFullScreenSearchBar
import androidx.compose.material3.FloatingToolbarDefaults
import androidx.compose.material3.HorizontalFloatingToolbar
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.InputChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.PlainTooltip
import androidx.compose.material3.SearchBarDefaults
import androidx.compose.material3.SearchBarValue
import androidx.compose.material3.SheetValue
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TooltipBox
import androidx.compose.material3.TooltipAnchorPosition
import androidx.compose.material3.TooltipDefaults
import androidx.compose.material3.rememberBottomSheetState
import androidx.compose.material3.rememberSearchBarState
import androidx.compose.material3.rememberTooltipState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun RecordQueryToolbar(query: RecordListQuery, modifier: Modifier = Modifier,
  onSearch: () -> Unit, onFilters: () -> Unit) {
  HorizontalFloatingToolbar(expanded = true, modifier = modifier.testTag("records-query-toolbar"),
    colors = FloatingToolbarDefaults.standardFloatingToolbarColors()) {
    RecordQueryTool(R.drawable.ic_search, stringResource(R.string.record_search_open),
      query.searchesText, "records-search-toggle", onSearch)
    RecordQueryTool(R.drawable.ic_filter, stringResource(R.string.record_filter_open),
      query.winner != RecordWinner.All || query.order != RecordOrder.Newest,
      "records-filter-toggle", onFilters)
  }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun RecordQueryTool(@DrawableRes icon: Int, label: String, active: Boolean,
  tag: String, onClick: () -> Unit) {
  TooltipBox(positionProvider = TooltipDefaults.rememberTooltipPositionProvider(TooltipAnchorPosition.Above),
    tooltip = { PlainTooltip { Text(label) } }, state = rememberTooltipState()) {
    IconButton(onClick = onClick, modifier = Modifier.size(48.dp).testTag(tag)
      .semantics { selected = active }) {
      Icon(painterResource(icon), contentDescription = label,
        tint = if (active) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
    }
  }
}

@Composable
internal fun RecordActiveQueryChips(query: RecordListQuery, modifier: Modifier = Modifier,
  onEvent: (BootstrapScreen.Event) -> Unit) {
  if (query.isDefault) return
  FlowRow(modifier.fillMaxWidth().testTag("records-active-query"),
    horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small),
    verticalArrangement = Arrangement.spacedBy(4.dp)) {
    if (query.searchesText) {
      val label = stringResource(R.string.record_query_text_chip, query.text)
      val removeLabel = stringResource(R.string.record_query_remove_text, query.text)
      InputChip(selected = true, onClick = { onEvent(BootstrapScreen.Event.SearchRecords("")) },
        label = { Text(label, maxLines = 1, overflow = TextOverflow.Ellipsis) },
        trailingIcon = { Icon(painterResource(R.drawable.ic_close), null, Modifier.size(18.dp)) },
        modifier = Modifier.heightIn(min = 48.dp).testTag("records-query-text-chip")
          .semantics { contentDescription = removeLabel })
    }
    if (query.winner != RecordWinner.All) {
      val name = query.winner.nameInRecord.orEmpty()
      val removeLabel = stringResource(R.string.record_query_remove_winner, name)
      InputChip(selected = true, onClick = { onEvent(BootstrapScreen.Event.SelectWinner(RecordWinner.All)) },
        label = { Text(stringResource(R.string.record_list_winner, name)) },
        leadingIcon = { ShittimParticipantAvatar(name, query.winner.slot, size = 24.dp) },
        trailingIcon = { Icon(painterResource(R.drawable.ic_close), null, Modifier.size(18.dp)) },
        modifier = Modifier.heightIn(min = 48.dp).testTag("records-query-winner-chip")
          .semantics { contentDescription = removeLabel })
    }
    if (query.order != RecordOrder.Newest) {
      val removeLabel = stringResource(R.string.record_query_remove_order)
      InputChip(selected = true, onClick = { onEvent(BootstrapScreen.Event.SelectOrder(RecordOrder.Newest)) },
        label = { Text(stringResource(R.string.record_sort_oldest)) },
        trailingIcon = { Icon(painterResource(R.drawable.ic_close), null, Modifier.size(18.dp)) },
        modifier = Modifier.heightIn(min = 48.dp).testTag("records-query-order-chip")
          .semantics { contentDescription = removeLabel })
    }
    TextButton(onClick = { onEvent(BootstrapScreen.Event.ClearRecordQuery) },
      modifier = Modifier.testTag("records-query-reset")) {
      Text(stringResource(R.string.record_search_reset))
    }
  }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun RecordFilterSheet(query: RecordListQuery, onDismiss: () -> Unit,
  onEvent: (BootstrapScreen.Event) -> Unit) {
  ModalBottomSheet(onDismissRequest = onDismiss, modifier = Modifier.testTag("records-filter-sheet"),
    sheetState = rememberBottomSheetState(initialValue = SheetValue.Hidden,
      enabledValues = setOf(SheetValue.Hidden, SheetValue.Expanded))) {
    Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()).testTag("records-filter-content")
      .padding(ShittimSpacing.Medium), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
      ShittimSectionHeading(stringResource(R.string.record_filter_sheet_title))
      RecordQueryControls(query, showSearch = false, showFilters = true, onEvent = onEvent)
      TextButton(onClick = onDismiss, modifier = Modifier.align(Alignment.End).testTag("records-filter-done")) {
        Text(stringResource(R.string.record_filter_sheet_done))
      }
    }
  }
}

/** Compose only for an explicit search opening; returning from a record must leave this absent. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun RecordFullScreenSearch(query: RecordListQuery, onDismiss: () -> Unit,
  onEvent: (BootstrapScreen.Event) -> Unit,
  content: @Composable ColumnScope.((BootstrapScreen.Event) -> Unit) -> Unit) {
  // The standard saved TextFieldState would serialize private search text into SavedState.
  val textField = remember { TextFieldState(query.text) }
  val searchBar = rememberSearchBarState(initialValue = SearchBarValue.Expanded)
  val currentQuery by rememberUpdatedState(query.text)
  val currentEvent by rememberUpdatedState(onEvent)
  val currentDismiss by rememberUpdatedState(onDismiss)
  val focusManager = LocalFocusManager.current
  val keyboard = LocalSoftwareKeyboardController.current
  val scope = rememberCoroutineScope()
  var dismissed by remember { mutableStateOf(false) }
  var forwardedText by remember { mutableStateOf(query.text) }
  val pendingEdits = remember { mutableListOf<String>() }

  val dismiss = {
    if (!dismissed) {
      dismissed = true
      focusManager.clearFocus(force = true)
      keyboard?.hide()
      currentDismiss()
    }
  }
  // An acknowledgement of an earlier edit must not overwrite a more recent keystroke.
  LaunchedEffect(query.text) {
    val acknowledged = pendingEdits.indexOf(query.text)
    if (acknowledged >= 0) pendingEdits.subList(0, acknowledged + 1).clear()
    else if (textField.text.toString() != query.text) {
      pendingEdits.clear()
      forwardedText = query.text
      textField.setTextAndPlaceCursorAtEnd(query.text)
    }
  }
  LaunchedEffect(textField) {
    snapshotFlow { textField.text.toString() }.distinctUntilChanged().collect { text ->
      if (text != forwardedText) {
        forwardedText = text
        if (text != currentQuery) {
          pendingEdits.add(text)
          currentEvent(BootstrapScreen.Event.SearchRecords(text))
        }
      }
    }
  }
  // Material handles the dialog's Back gesture and keyboard collapse; remove disclosure afterwards.
  LaunchedEffect(searchBar) {
    snapshotFlow { searchBar.currentValue }.distinctUntilChanged().collect { value ->
      if (value == SearchBarValue.Collapsed) dismiss()
    }
  }
  DisposableEffect(Unit) {
    onDispose {
      keyboard?.hide()
      focusManager.clearFocus(force = true)
    }
  }
  val resultEvent: (BootstrapScreen.Event) -> Unit = { event ->
    when (event) {
      is BootstrapScreen.Event.OpenRecord -> scope.launch {
        searchBar.snapTo(0f)
        dismiss()
        currentEvent(event)
      }
      is BootstrapScreen.Event.SearchRecords -> {
        forwardedText = event.text
        textField.setTextAndPlaceCursorAtEnd(event.text)
        currentEvent(event)
      }
      BootstrapScreen.Event.ClearRecordQuery -> {
        forwardedText = ""
        textField.clearText()
        currentEvent(event)
      }
      else -> currentEvent(event)
    }
  }
  ExpandedFullScreenSearchBar(state = searchBar, modifier = Modifier.testTag("records-search-screen"),
    inputField = {
      SearchBarDefaults.InputField(textFieldState = textField, searchBarState = searchBar,
        onSearch = { keyboard?.hide(); focusManager.clearFocus(force = true) },
        placeholder = { Text(stringResource(R.string.record_search_label)) },
        leadingIcon = {
          IconButton(onClick = { scope.launch { searchBar.animateToCollapsed() } },
            modifier = Modifier.testTag("records-search-close")) {
            Icon(painterResource(R.drawable.ic_arrow_back), stringResource(R.string.record_search_close))
          }
        },
        trailingIcon = if (textField.text.isNotEmpty()) {
          {
            IconButton(onClick = { textField.clearText() }, modifier = Modifier.testTag("records-search-clear")) {
              Icon(painterResource(R.drawable.ic_close), stringResource(R.string.record_search_clear_text))
            }
          }
        } else null,
        keyboardOptions = KeyboardOptions(autoCorrectEnabled = false, imeAction = ImeAction.Search),
        lineLimits = TextFieldLineLimits.SingleLine, modifier = Modifier.testTag("record-search"))
    }) {
    content(resultEvent)
  }
}
