package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.net.Uri
import java.io.ByteArrayOutputStream
import java.time.Instant

/** An in-app selection hint, never a URI accepted by a production image importer. */
internal val DEMO_SAMPLE_IMAGE_URI: Uri = Uri.parse("review-demo:sample-image")

internal class DemoRecord(val entry: RecordListEntry, val preview: RecordPreview)

/** Authored examples only; no Discord exports, remote image URLs, or generated AI responses. */
internal object DemoFixtures {
  private val names = listOf("アロナ", "プラナ", "安倍晋三AI")
  private val slots = listOf("participant-a", "participant-b", "participant-c")
  private val samples: List<ByteArray> by lazy { (0..2).map(::drawSample) }

  private fun drawSample(variant: Int): ByteArray {
    val bitmap = Bitmap.createBitmap(640, 480, Bitmap.Config.ARGB_8888)
    try {
      val canvas = Canvas(bitmap)
      val paint = Paint(Paint.ANTI_ALIAS_FLAG)
      canvas.drawColor(listOf(Color.rgb(225, 242, 248), Color.rgb(248, 231, 239), Color.rgb(235, 232, 250))[variant])
      paint.color = Color.rgb(245, 207, 101)
      canvas.drawCircle(520f - variant * 80f, 95f, 46f, paint)
      paint.color = Color.rgb(115, 182, 139)
      canvas.drawOval(-130f, 280f, 480f, 690f, paint)
      paint.color = Color.rgb(76, 139, 130)
      canvas.drawOval(230f, 310f, 830f, 700f, paint)
      paint.color = Color.rgb(255, 249, 229)
      canvas.drawRoundRect(170f, 290f, 460f, 425f, 18f, 18f, paint)
      paint.color = Color.rgb(226, 139, 131)
      canvas.drawCircle(240f, 345f, 26f, paint)
      paint.color = Color.rgb(109, 157, 204)
      canvas.drawCircle(320f, 365f, 26f, paint)
      paint.color = Color.rgb(241, 184, 98)
      canvas.drawCircle(390f, 340f, 26f, paint)
      return ByteArrayOutputStream().use { output ->
        check(bitmap.compress(Bitmap.CompressFormat.PNG, 100, output))
        output.toByteArray()
      }
    } finally { bitmap.recycle() }
  }

  fun sampleImageBytes(image: RecordAttachment = sampleImage): ByteArray {
    val index = sampleImages.indexOfFirst { it.sameImage(image) }
    check(index >= 0)
    return samples[index].copyOf()
  }

  val sampleImages: List<RecordAttachment> by lazy {
    samples.mapIndexed { index, bytes ->
      RecordAttachment("0000000${index + 1}-0000-4000-8000-abcdefabcdef", "image/png",
        bytes.size, attachmentDigest(bytes), 640, 480)
    }
  }
  val sampleImage: RecordAttachment get() = sampleImages.first()

  val records: List<DemoRecord> by lazy {
    val questions = listOf(
      "架空の相談：3人で公園のピクニックを楽しむ方法を考えてください。",
      "架空の相談：雨の日に読書会を開くなら、どんな準備が必要ですか？",
      "架空の相談：色紙で小さな街を作る工作会を企画してください。",
      "架空の相談：星空をテーマにしたお茶会の進め方を考えてください。",
      "架空の相談：短い散歩と休憩を組み合わせた休日を計画してください。",
      "架空の相談：机の上を整理して勉強するための工夫を提案してください。",
    )
    questions.mapIndexed { index, question ->
      val id = "demo_record_${index + 1}".padEnd(43, '0')
      val winner = index % names.size
      val images = when (index) { 0 -> listOf(sampleImage); 2 -> sampleImages; else -> emptyList() }
      DemoRecord(
        RecordListEntry(id, question, "デモの先生${index % 2 + 1}",
          RecordAvatar(null, if (index % 2 == 0) "blue" else "pink"), Instant.parse("2026-10-01T09:00:00Z").minusSeconds(index * 86_400L),
          names[winner], slots[winner], images.size),
        result(question, images, winner, when (index) {
          4 -> RecordAffectionReasonStatus.NOT_RECORDED
          5 -> RecordAffectionReasonStatus.UNAVAILABLE
          else -> RecordAffectionReasonStatus.AVAILABLE
        }),
      )
    }
  }

  fun result(question: String, images: List<RecordAttachment>, winner: Int = 0,
    reasonStatus: RecordAffectionReasonStatus = RecordAffectionReasonStatus.AVAILABLE): RecordPreview {
    val opinions = names.indices.map { index ->
      RecordOpinion(names[index], "架空の提案${index + 1}の要約",
        "これはデモ用に用意した初回意見です。準備・休憩・楽しむ時間を分けて計画します。",
        "デモの最終提案${index + 1}",
        "これはデモ用の最終提案です。無理のない順番で進め、途中で休憩します。",
        slots[index])
    }
    val candidates = names.indices.map { voter -> if (voter == winner) (winner + 1) % names.size else winner }
    val votes = names.indices.map { voter ->
      val candidate = candidates[voter]
      val reason = "架空の採点理由：準備と楽しむ時間の配分が分かりやすいためです。"
      val assessments = names.indices.filter { it != voter }.map { assessed ->
        val points = if (assessed == candidate) 4 else 3
        RecordAssessment(names[assessed], points, points, points, points, points,
          if (assessed == candidate) reason else "架空の採点理由：進め方をもう少し具体化できます。",
          points * 5, slots[assessed])
      }
      RecordVote(names[voter], names[candidate], reason, assessments, slots[voter], slots[candidate])
    }
    val voting = RecordVoting(votes, names.indices.map { index ->
      RecordVoteCount(names[index], candidates.count { it == index }, slots[index])
    }, VoteDecisionMethod.MAJORITY, false)
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, names.indices.map { index ->
      val before = 420 + index * 25
      val delta = if (index == winner) 6 else 3
      RecordAffectionChange(names[index], before, delta, delta, before + delta, slots[index],
        "架空の親愛度理由：みんなで楽しむ計画を一緒に考えられたためです。"
          .takeIf { reasonStatus == RecordAffectionReasonStatus.AVAILABLE }, reasonStatus)
    })
    return RecordPreview(question,
      "## デモの結果\n\nこの結果は、操作確認のためにあらかじめ用意した架空の内容です。" +
        "入力した議題や画像をAIが分析した結果ではありません。\n\n" +
        "1. 準備することを決めます。\n2. 途中で休憩します。\n3. 最後に感想を共有します。",
      names[winner], opinions, voting,
      "デモの勝利コメント：みんなで楽しく進めましょう。",
      listOf("デモ：必要なものをリストにします。", "デモ：休憩の時間を決めます。"),
      listOf("このデモは外部AIの生成・Discord投稿・本番の親愛度変更を行いません。"),
      affection, slots[winner], images.map { it.cached() })
  }
}
