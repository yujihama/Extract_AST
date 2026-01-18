## テストデータ概要（usecase.md 検証用）

このフォルダの文書は、`design/usecase.md` の代表ユースケース（準拠評価 / ギャップ分析 / 差分・改訂影響 / トレーサビリティ / 抽出・構造化 / 品質点検）を手元で検証できるように作ったサンプルです。
すべて架空の内容です。

---

## 想定シナリオ（入力の組み合わせ）

### 1) 基準 + 対象（準拠評価 / ギャップ）
- 基準（規程・要求）: `standard_security_baseline.txt`
- 対象（社内規程）: `policy_it_security.txt`
- 補助（手順/運用）: `procedure_access_request.txt`, `procedure_vendor_onboarding.txt`, `procedure_key_management.txt`
- ねらい:
  - 「未整備/要改善」になりやすい差分（監査ログ、委託先管理、鍵管理、例外承認など）をわざと入れています。

### 2) 要求 + 設計 + テスト（トレーサビリティ / 欠落検出）
- 要求: `requirements_product_x.txt`
- 設計: `design_product_x.txt`
- テスト仕様: `test_spec_product_x.txt`
- ねらい:
  - 要求→設計→テストの対応が「一部欠落」「部分対応」「用語ゆれ」で崩れる例を含みます。

### 3) v1 + v2（差分・改訂影響）
- 旧: `contract_service_v1.txt`
- 新: `contract_service_v2.txt`
- 追加（時系列）: `contract_service_v3.txt`
- ねらい:
  - SLO/違約金/再委託/ログ保全/通知期限などの改訂点を入れています。

### 4) 用語集（品質点検）
- 用語定義: `glossary_terms.txt`
- ねらい:
  - 文書間で「同じ意味の別名」「同名で別定義」を起こしやすい語を入れています。

### 5) 生成・ドラフト（テンプレ記入）
- 入力: `company_profile.txt`, `product_overview_x.txt`, `risk_register_q4.txt`
- テンプレ: `template_disclosure_draft.md`

### 6) 抽出・構造化（義務/禁止/期限/例外）
- 入力: `privacy_addendum_sample.txt`
- ねらい: 期限・例外・通知義務・罰則（違約金）など抽出対象を散らしてあります。

### 7) 要約（役割別）
- 入力: `incident_postmortem_2026_01.txt`

### 8) 検索・Q&A（根拠付き）
- 入力: `faq_product_x.txt`, `policy_it_security.txt`, `contract_service_v2.txt`
- 質問リスト: `questions_product_x.txt`

### 9) 翻訳・平易化
- 入力（原文・日本語）: `jp_security_notice.txt`

### 10) 単一文書の品質点検（矛盾/参照切れ）
- 入力: `single_policy_with_issues.txt`

---

## PowerShellでの配置確認

```powershell
Get-ChildItem .\data\input\ -File | Select-Object Name,Length
```

