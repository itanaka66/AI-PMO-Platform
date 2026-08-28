以下のアクションアイテムを、課題管理システムに登録できる形へ正規化してください。

本日: {{ inputs.today }}
参加者: {{ inputs.participants }}

アクションアイテム:
{{ inputs.action_items }}

次の JSON オブジェクトのみを出力してください。

{
  "items": [
    {"summary": "課題タイトル（命令形・50 文字以内）",
     "description": "背景と完了条件",
     "assignee": "担当者名。不明なら null",
     "due_date": "YYYY-MM-DD 形式。相対表現は本日基準で解決する。不明なら null",
     "priority": "High / Medium / Low",
     "confidence": 0.0}
  ]
}

重要:
- 「来週金曜」などの相対表現は本日を基準に実日付へ変換すること。
- 担当者が曖昧な場合は推測せず null にし、confidence を下げること。
- 実際にはタスクでないもの（単なる感想・確認済みの事実）は除外すること。
