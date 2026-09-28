package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.participantVisualSlot
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import kotlin.math.hypot
import kotlin.math.roundToInt

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun RecordVotingPanel(voting: RecordVoting, winnerName: String, winnerSlot: String?,
  motionKey: String? = null, playedSections: Set<String> = emptySet(),
  onSectionSeen: (String) -> Unit = {}) {
  var selectedVote by remember(voting) { mutableStateOf<RecordVote?>(null) }
  val played = motionKey == null || motionKey in playedSections || !ValueAnimator.areAnimatorsEnabled()
  val progress by animateFloatAsState(if (played) 1f else 0f,
    animationSpec = tween(1_000), label = "vote routes")
  ShittimSectionHeading(stringResource(R.string.record_votes))
  val countSlots = voting.counts.map { participantVisualSlot(it.participantName, it.participantSlot) }
  val canDrawGraph = voting.counts.size == 3 && voting.votes.size == 3 &&
    countSlots.none { it == null } && countSlots.toSet().size == 3 &&
    voting.votes.all { vote ->
      val voter = participantVisualSlot(vote.voterName, vote.voterSlot)
      val candidate = participantVisualSlot(vote.candidateName, vote.candidateSlot)
      voter != candidate && voter in countSlots && candidate in countSlots
    }
  if (LocalDensity.current.fontScale >= 1.5f || !canDrawGraph) {
    VoteRows(voting, winnerName, winnerSlot) { selectedVote = it }
  } else BoxWithConstraints(Modifier.fillMaxWidth()) {
    if (maxWidth < 300.dp) {
      VoteRows(voting, winnerName, winnerSlot) { selectedVote = it }
    } else {
      val nodes = listOf("participant-a", "participant-b", "participant-c").mapIndexed { index, slot ->
        voting.counts.firstOrNull { participantVisualSlot(it.participantName, it.participantSlot) == slot }
          ?: voting.counts[index]
      }
      val colors = nodes.map { shittimParticipantColor(it.participantName, it.participantSlot) }
      BoxWithConstraints(Modifier.fillMaxWidth().height(232.dp)
        .markRecordSectionSeen(motionKey, played, onSectionSeen)) {
        val x = listOf(maxWidth / 2, maxWidth * 0.2f, maxWidth * 0.8f)
        val y = listOf(36.dp, 162.dp, 162.dp)
        Canvas(Modifier.matchParentSize()) {
          val centers = listOf(Offset(size.width / 2, 36.dp.toPx()),
            Offset(size.width * 0.2f, 162.dp.toPx()), Offset(size.width * 0.8f, 162.dp.toPx()))
          voting.votes.forEachIndexed { index, vote ->
            val source = nodes.indexOfFirst { participantVisualSlot(it.participantName,
              it.participantSlot) == participantVisualSlot(vote.voterName, vote.voterSlot) }
            val target = nodes.indexOfFirst { participantVisualSlot(it.participantName,
              it.participantSlot) == participantVisualSlot(vote.candidateName, vote.candidateSlot) }
            if (source >= 0 && target >= 0 && source != target) {
              drawVoteArrow(centers[source], centers[target],
                (progress * 3f - index).coerceIn(0f, 1f), colors[source],
                if (selectedVote == null || selectedVote === vote) 1f else 0.22f)
            }
          }
        }
        nodes.forEachIndexed { index, node ->
          val vote = voting.votes.firstOrNull { participantVisualSlot(it.voterName,
            it.voterSlot) == participantVisualSlot(node.participantName, node.participantSlot) }
            ?: voting.votes[index]
          val pulse = voting.votes.indices.maxOf { voteIndex ->
            val candidate = voting.votes[voteIndex]
            if (participantVisualSlot(candidate.candidateName, candidate.candidateSlot) !=
              participantVisualSlot(node.participantName, node.participantSlot)) 0f
            else (1f - kotlin.math.abs(progress * 3f - voteIndex - 1f) * 4f).coerceIn(0f, 1f)
          }
          VoteNode(node, winnerName, winnerSlot, pulse,
            Modifier.offset(x = x[index] - 42.dp, y = y[index] - 28.dp)
              .width(84.dp).testTag("vote-person-$index")) {
            selectedVote = vote
          }
        }
      }
    }
  }
  val explanation = when (voting.decidedBy) {
    VoteDecisionMethod.MAJORITY -> R.string.record_decided_majority
    VoteDecisionMethod.COMPOSITE_SCORE -> R.string.record_decided_composite
    VoteDecisionMethod.TIE_LOTTERY -> R.string.record_decided_lottery
    null -> if (voting.legacyTieBreakApplied) R.string.record_decided_legacy_tie else null
  }
  if (explanation != null) Text(stringResource(explanation), style = MaterialTheme.typography.bodyMedium)
  selectedVote?.let { vote ->
    ModalBottomSheet(onDismissRequest = { selectedVote = null }) { VoteDetail(vote) }
  }
}

@Composable
private fun VoteNode(count: RecordVoteCount, winnerName: String, winnerSlot: String?,
  pulse: Float, modifier: Modifier, onClick: () -> Unit) {
  val isWinner = participantVisualSlot(count.participantName, count.participantSlot) ==
    participantVisualSlot(winnerName, winnerSlot)
  Column(modifier.clickable(onClick = onClick).scale(1f + pulse * 0.11f),
    horizontalAlignment = Alignment.CenterHorizontally,
    verticalArrangement = Arrangement.spacedBy(2.dp)) {
    ShittimParticipantAvatar(count.participantName, count.participantSlot, size = 56.dp,
      crowned = isWinner)
    Text(count.participantName, style = MaterialTheme.typography.labelMedium,
      color = shittimParticipantColor(count.participantName, count.participantSlot))
    Surface(shape = MaterialTheme.shapes.small, color = MaterialTheme.colorScheme.surfaceContainerHigh) {
      Text(stringResource(R.string.record_vote_badge, count.count),
        modifier = Modifier.padding(horizontal = 7.dp, vertical = 2.dp),
        style = MaterialTheme.typography.labelSmall)
    }
  }
}

@Composable
private fun VoteRows(voting: RecordVoting, winnerName: String, winnerSlot: String?,
  onClick: (RecordVote) -> Unit) {
  voting.votes.forEachIndexed { index, vote ->
    val count = voting.counts.firstOrNull { participantVisualSlot(it.participantName,
      it.participantSlot) == participantVisualSlot(vote.voterName, vote.voterSlot) }
    Surface(onClick = { onClick(vote) }, modifier = Modifier.fillMaxWidth().testTag("vote-route-$index"),
      shape = MaterialTheme.shapes.medium, color = MaterialTheme.colorScheme.surfaceContainerHigh) {
      Row(Modifier.padding(ShittimSpacing.Small), verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        ShittimParticipantAvatar(vote.voterName, vote.voterSlot, size = 36.dp)
        Text("→", style = MaterialTheme.typography.titleMedium)
        ShittimParticipantAvatar(vote.candidateName, vote.candidateSlot, size = 36.dp,
          crowned = participantVisualSlot(vote.candidateName, vote.candidateSlot) ==
            participantVisualSlot(winnerName, winnerSlot))
        Column {
          Text(stringResource(R.string.record_vote_route, vote.voterName, vote.candidateName),
            style = MaterialTheme.typography.labelMedium)
          if (count != null) Text(stringResource(R.string.record_vote_badge, count.count),
            style = MaterialTheme.typography.labelSmall)
        }
      }
    }
  }
}

@Composable
private fun VoteDetail(vote: RecordVote) {
  Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState())
    .padding(horizontal = ShittimSpacing.Large, vertical = ShittimSpacing.Medium),
    verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
    Row(verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
      ShittimParticipantAvatar(vote.voterName, vote.voterSlot)
      Text("→", style = MaterialTheme.typography.titleMedium)
      ShittimParticipantAvatar(vote.candidateName, vote.candidateSlot)
      Text(stringResource(R.string.record_vote_route, vote.voterName, vote.candidateName),
        style = MaterialTheme.typography.titleSmallEmphasized)
    }
    Text(vote.reason, style = MaterialTheme.typography.bodyLarge)
    vote.assessments?.forEach { assessment ->
      ShittimSectionHeading(stringResource(R.string.record_assessment_total,
        assessment.candidateName, assessment.total), style = MaterialTheme.typography.titleSmallEmphasized)
      listOf(
        ("✦" to stringResource(R.string.record_assessment_entertainment, assessment.entertainment)) to assessment.entertainment,
        ("◈" to stringResource(R.string.record_assessment_character, assessment.character)) to assessment.character,
        ("◇" to stringResource(R.string.record_assessment_originality, assessment.originality)) to assessment.originality,
        ("↗" to stringResource(R.string.record_assessment_responsiveness, assessment.responsiveness)) to assessment.responsiveness,
        ("⇄" to stringResource(R.string.record_assessment_interaction, assessment.interaction)) to assessment.interaction,
      ).forEach { (symbolAndLabel, score) ->
        Row(horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
          Text(symbolAndLabel.first)
          Text(symbolAndLabel.second, style = MaterialTheme.typography.labelMedium)
        }
        LinearProgressIndicator(progress = { score / 5f }, modifier = Modifier.fillMaxWidth())
      }
      Text(assessment.reason, style = MaterialTheme.typography.bodyMedium)
    }
  }
}

private fun DrawScope.drawVoteArrow(from: Offset, to: Offset, progress: Float,
  color: Color, alpha: Float) {
  if (progress <= 0f) return
  val delta = to - from
  val distance = hypot(delta.x, delta.y)
  if (distance < 1f) return
  val unit = delta / distance
  val start = from + unit * 34.dp.toPx()
  val end = to - unit * 34.dp.toPx()
  val normal = Offset(-unit.y, unit.x)
  val control = (start + end) * 0.5f + normal * 17.dp.toPx()
  fun point(t: Float): Offset = start * ((1f - t) * (1f - t)) +
    control * (2f * (1f - t) * t) + end * (t * t)
  val segments = (progress * 24).roundToInt().coerceAtLeast(1)
  val path = Path().apply { moveTo(start.x, start.y) }
  for (index in 1..segments) {
    val location = point(progress * index / segments)
    path.lineTo(location.x, location.y)
  }
  val stroke = color.copy(alpha = alpha)
  drawPath(path, stroke, style = Stroke(2.5.dp.toPx(), cap = StrokeCap.Round))
  val tip = point(progress)
  val tangent = (control - start) * (2f * (1f - progress)) +
    (end - control) * (2f * progress)
  val tangentLength = hypot(tangent.x, tangent.y)
  if (tangentLength > 0f) {
    val forward = tangent / tangentLength
    val side = Offset(-forward.y, forward.x)
    val base = tip - forward * 10.dp.toPx()
    drawLine(stroke, tip, base + side * 5.dp.toPx(), strokeWidth = 2.5.dp.toPx())
    drawLine(stroke, tip, base - side * 5.dp.toPx(), strokeWidth = 2.5.dp.toPx())
  }
}
