# 翻訳および重要事実保持チェックテンプレート

ドキュメントID: d1
ファイル名: jp_security_notice.txt
作成日: 2026-01-18
担当者: AI翻訳エージェント

## 1. 原文（日本語）
【セキュリティ通知（原文）】（架空）

当社は2026年1月に、外部委託先による作業ミスに起因する障害を検知しました。
影響範囲は限定的でしたが、複数の顧客で一時的なAPIエラーが発生しました。
原因は、設定変更のレビュー不足と監視アラートの遅延でした。
当社は再発防止策として、二重承認と監視強化を実施します。
現在、影響評価と追加対応を進めています。

## 2. 英訳（翻訳本文）
Security Notice (Original) [fictional]

In January 2026, we detected an incident caused by a work error by an external contractor.
Although the scope of impact was limited, temporary API errors occurred for multiple customers.
The cause was insufficient review of configuration changes and delayed monitoring alerts.
As recurrence-prevention measures, we will implement dual approval and strengthen monitoring.
We are currently conducting impact assessments and proceeding with additional remediation actions.

## 3. 抽出された重要事実（カテゴリ別）
- 原因 (Causes):
  - 1) 外部委託先の作業ミスによる障害の発生（原文行3）
  - 2) 設定変更のレビュー不足（原文行5）
  - 3) 監視アラートの遅延（原文行5）
- 影響 (Impacts):
  - 1) 影響範囲は限定的であった（原文行4）
  - 2) 複数の顧客で一時的なAPIエラーが発生した（原文行4）
- 対策 / 対応 (Mitigations / Actions):
  - 1) 二重承認の実施（原文行6）
  - 2) 監視の強化（原文行6）
  - 3) 現在、影響評価と追加対応を進めている（原文行7）

> 注: 事実は原文に明示されている内容を短く箇条書きにしました。

## 4. 事実保持チェックリスト
以下の行は、抽出した各事実について、原文の該当箇所、英訳の該当箇所、保持判定、備考を記入する。

| カテゴリ | 事実（日本語 原文） | 英訳（該当文） | 保持されているか (Yes/No) | 備考 |
|---|---|---|---|---|
| 原因 | 外部委託先による作業ミスに起因する障害の検知（原文行3） | "we detected an incident caused by a work error by an external contractor." | Yes | 主語（当社）と因果関係（外部委託先の作業ミス→障害）は維持されている。 |
| 原因 | 設定変更のレビュー不足（原文行5） | "insufficient review of configuration changes" | Yes | 意味と因果（レビュー不足が原因の一つ）は保持。 |
| 原因 | 監視アラートの遅延（原文行5） | "delayed monitoring alerts" | Yes | 意味保持。 |
| 影響 | 影響範囲は限定的（原文行4） | "the scope of impact was limited" | Yes | 範囲の限定性は保持。 |
| 影響 | 複数の顧客で一時的なAPIエラーが発生（原文行4） | "temporary API errors occurred for multiple customers" | Yes | 影響の種類と対象（複数の顧客、APIエラー、一時的）が保持。 |
| 対策 | 二重承認の実施（原文行6） | "implement dual approval" | Yes | 対策内容は明確に翻訳。 |
| 対策 | 監視の強化（原文行6） | "strengthen monitoring" | Yes | 意図通り。 |
| 対策 | 影響評価と追加対応を進めている（原文行7） | "conducting impact assessments and proceeding with additional remediation actions" | Yes | 現状の進捗を示す表現（進行中）は保持。 |

※ 保持判定は、意味・因果関係・影響範囲・時制などが原文と同等であるかで判定しました。

## 5. 翻訳品質メモ
- 用語の扱い（専門用語、固有名詞）: 特定の固有名詞は本文に含まれていないため、一般的な技術用語（API, monitoring, configuration change）は直訳で問題ありません。"二重承認"は"dual approval"または"two-step approval"のいずれも可。文脈では "dual approval" を採用しました。
- 曖昧さが生じた箇所: 特になし。原文は簡潔で明確に因果関係と対策を述べています。
- 推奨する修正（必要なら）: "二重承認"をより明確にする場合は"two-person approval"や"two-step approval"の補足を検討してください（運用上の意味合いにより適切な訳語を選択）。

## 6. 最終評価
- 重要事実の保持: All
- 推奨対応（追加確認や修正が必要なら）: 特になし。必要に応じて"二重承認"の具体的実装（誰が関与するか）を明記すると受け手の理解が高まります。

---

（このテンプレートは、翻訳者が英訳とともに事実保持を検証するためのチェックリストです。）
