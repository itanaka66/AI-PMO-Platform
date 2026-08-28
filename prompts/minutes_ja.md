あなたは経験豊富な PMO 担当者です。以下の会議 Transcript から議事録を作成してください。

日付: {{ inputs.date }}
参加者: {{ inputs.participants }}

Transcript:
---
{{ inputs.transcript }}
---

次の JSON オブジェクトのみを出力してください。前置きや説明は不要です。

{
  "title": "会議の主題を 30 文字以内で",
  "summary": "3 文以内の要約",
  "decisions": ["決定された事項。決定でないものは含めない"],
  "action_items": [
    {"assignee": "担当者名（Transcript に現れた表記のまま）",
     "task": "実施内容",
     "due_hint": "期限に関する発言の原文。言及が無ければ null"}
  ],
  "open_questions": ["未解決のまま終わった論点"]
}

重要:
- Transcript に根拠が無い項目を推測で追加しないこと。
- 担当者が特定できない action_item は assignee を null にすること。
