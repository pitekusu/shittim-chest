package dev.pitekusu.shittim.records

/** Authored, network-free examples. The image is the existing hand-drawn review-demo sample. */
internal fun momotalkSampleState(openRoom: Boolean = false): MomotalkUiState {
  val week = MomotalkWeek("2026-10-12", "2026-10-11T15:00:00Z", "2026-10-18T15:00:00Z", "2026-10-19T03:00:00Z")
  val room = MomotalkRoomSummary("fictional-room-1".padEnd(43, '0'),
    MomotalkRequester("架空の先生", RecordAvatar(null, "cyan")), 12, "ready")
  val participants = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
    MomotalkParticipant(MOMOTALK_PARTICIPANTS[index], name, RecordAvatar(null, listOf("cyan", "pink", "lavender")[index]))
  }
  val messages = listOf(
    "今週もいろいろなお話ができましたね。先生のピクニックの相談、楽しそうでした！",
    "お弁当を分け合えるように、準備する順番も考えました。休憩の時間も大切です。",
    "みんなで協力して、一つずつ進めていきましょう。帰り道のことまで考えておくと安心ですね。",
    "紙で小さな庭を作るアイデアも、かわいくできそうです！",
    "雨の日には、できた庭を眺めながら読書会もできますね。",
    "それでは、今週の記念に一枚。来週も楽しくお話ししましょう。",
  ).mapIndexed { index, text -> MomotalkMessage(index + 1, MOMOTALK_PARTICIPANTS[index % 3], text) }
  val image = MomotalkImage("participant-a", "happy",
    participants.map { MomotalkImageSubject(it.slot, "happy") }, "ready", bytes = DemoFixtures.sampleImageBytes())
  val detail = MomotalkRoomDetail(week = week, room = room, participants = participants, messages = messages, images = listOf(image))
  return MomotalkUiState(selectedWeek = if (openRoom) week.weekId else null,
    selectedRoom = if (openRoom) room.roomId else null, online = true,
    weeks = MomotalkResource(listOf(MomotalkWeeksPage(weeks = listOf(week)))),
    rooms = MomotalkResource(listOf(MomotalkRoomsPage(week = week, rooms = listOf(room)))),
    detail = MomotalkResource(detail))
}
