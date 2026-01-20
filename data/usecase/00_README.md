## テストデータ概要（usecase.md 検証用）

このフォルダの文書は、`design/usecase.md` の代表ユースケース（準拠評価 / ギャップ分析 / 差分・改訂影響 / トレーサビリティ / 抽出・構造化 / 品質点検）を手元で検証できるように作ったサンプルです。
すべて架空の内容です。

---

## 基本シナリオ（01〜10）

### 01_compliance: 準拠評価（セキュリティ）
- 基準: `standard_security_baseline.txt`
- 対象: `policy_it_security.txt`
- 補助: `procedure_access_request.txt`, `procedure_vendor_onboarding.txt`, `procedure_key_management.txt`
- ねらい: 監査ログ、委託先管理、鍵管理、例外承認などの差分を検出

### 02_gap: ギャップ分析（セキュリティ）
- 同上のデータで「未対応/部分対応/対応済み」の分類

### 03_change_impact: 差分・改訂影響（契約）
- 旧: `contract_service_v1.txt`
- 新: `contract_service_v2.txt`, `contract_service_v3.txt`
- ねらい: SLO/違約金/再委託/ログ保全/通知期限などの改訂点

### 04_traceability: トレーサビリティ
- 要求: `requirements_product_x.txt`
- 設計: `design_product_x.txt`
- テスト: `test_spec_product_x.txt`
- ねらい: 要求→設計→テストの対応欠落、部分対応、用語ゆれ

### 05_drafting: 生成・ドラフト作成
- 入力: `company_profile.txt`, `product_overview_x.txt`, `risk_register_q4.txt`
- テンプレ: `template_disclosure_draft.md`

### 06_extraction: 抽出・構造化
- 入力: `privacy_addendum_sample.txt`
- ねらい: 期限・例外・通知義務・罰則などの抽出

### 07_quality: 品質点検
- 入力: `single_policy_with_issues.txt`, `glossary_terms.txt`
- ねらい: 矛盾、参照切れ、用語ゆれの検出

### 08_summarization: 要約
- 入力: `incident_postmortem_2026_01.txt`
- ねらい: 経営向け/現場向け/監査向けの要点整理

### 09_qa: 検索・Q&A
- 入力: `faq_product_x.txt`, `policy_it_security.txt`, `contract_service_v2.txt`
- 質問: `questions_product_x.txt`

### 10_translation: 翻訳
- 入力: `jp_security_notice.txt`

---

## 拡張シナリオ: 新規ドメイン（11〜19）

### 11_healthcare_compliance: 準拠評価（医療）
- 基準: `healthcare_regulation_baseline.txt`（医療情報システム安全管理ガイドライン）
- 対象: `hospital_it_policy.txt`（病院IT規程）
- ねらい: MED-01〜MED-10への適合状況評価

### 12_aml_compliance: 準拠評価（金融AML）
- 基準: `aml_regulation_baseline.txt`（マネーローンダリング対策ガイドライン）
- 対象: `bank_aml_policy.txt`（銀行AML規程）
- ねらい: AML-01〜AML-10への適合状況評価

### 13_labor_gap: ギャップ分析（人事労務）
- 基準: `labor_law_baseline.txt`（労働法要件）
- 対象: `company_work_rules_v1.txt`（就業規則）
- ねらい: 育児休業、ハラスメント対応、割増賃金等のギャップ

### 14_qms_compliance: 準拠評価（製造品質）
- 基準: `iso9001_requirements.txt`（ISO 9001要求事項）
- 対象: `manufacturing_qms_manual.txt`（品質マニュアル）
- ねらい: 品質マネジメントシステムの適合性評価

### 15_clinical_trial_change: 差分分析（治験プロトコル）
- 旧: `clinical_trial_protocol_v1.txt`
- 新: `clinical_trial_protocol_v2.txt`
- ねらい: 用量漸増デザイン、対象患者基準、サイクル期間、バイオマーカー評価の変更とIRB/再同意への影響

### 16_work_rules_change: 差分分析（就業規則）
- 旧: `company_work_rules_v1.txt`
- 新: `company_work_rules_v2.txt`
- ねらい: フレックス、テレワーク、割増賃金、再雇用年齢、副業の変更と届出・周知への影響

### 17_credit_traceability: トレーサビリティ（与信審査システム）
- 要求: `credit_scoring_requirements.txt`
- 設計: `credit_scoring_design.txt`
- テスト: `credit_scoring_test_spec.txt`
- ねらい: 公平性テスト（REQ-N05）の網羅性など

### 18_spec_inspection_quality: 品質点検（製造）
- 仕様書: `product_spec_part_a.txt`
- 検査手順: `inspection_procedure.txt`
- ねらい: 仕様と検査手順の整合性、判定基準の不一致

### 19_consent_extraction: 抽出（医療同意書）
- 入力: `patient_consent_form.txt`
- ねらい: 研究目的、リスク、利益、費用、補償条件の構造化抽出

---

## 拡張シナリオ: 新規ユースケース（20〜21）

### 20_proposal_evaluation: 提案評価・ベンダー比較
- RFP: `rfp_cloud_system.txt`
- 提案A: `proposal_vendor_a.txt`
- 提案B: `proposal_vendor_b.txt`
- 評価基準: `proposal_evaluation_criteria.txt`
- ねらい: RFP要件への適合度評価、ベンダー比較表、スコアリング、推奨ベンダー選定

### 21_ma_dd_integration: M&A デューデリジェンス統合
- 対象会社: `ma_target_company_profile.txt`
- 法務DD: `ma_legal_dd_checklist.txt`, `ma_legal_dd_findings.txt`
- 財務DD: `ma_financial_dd_findings.txt`
- ねらい: リスク項目の統合・優先度付け、価格調整・表明保証への反映、対応事項リスト

---

## 実行方法（ユースケース一括実行）

`scripts/run_cli_scenarios.py` でまとめて実行できます。

### 事前準備

依存関係をインストールしてから実行してください（詳細はリポジトリ直下の `README.md` を参照）。

### dummy モード（APIキー不要）

```bash
# リポジトリ直下で実行（全21シナリオ）
python scripts/run_cli_scenarios.py --mode dummy
```

### real モード（APIキー必要）

`.env` または OS 環境変数に `OPENAI_API_KEY` もしくは `AZURE_OPENAI_API_KEY` を設定してから実行してください。

```bash
# 例: real を全シナリオ実行
python scripts/run_cli_scenarios.py --mode real --llm-complex-model gpt-5-mini
```

### 出力先（デフォルト）

- **dummy**: `data/usecase/cli_scenarios/`
- **real**: `data/usecase/cli_scenarios_real/`

各シナリオ配下に `create.json` / `execute.json` / `artifacts.json` / `template_filled.md` / `events.jsonl` などが出力されます。

### よく使うオプション

```bash
# 特定シナリオだけ実行（例: 基本の準拠評価 + 医療準拠評価）
python scripts/run_cli_scenarios.py --mode dummy --ids 01_compliance 11_healthcare_compliance

# 拡張シナリオのみ実行
python scripts/run_cli_scenarios.py --mode dummy --ids 11_healthcare_compliance 12_aml_compliance 13_labor_gap 14_qms_compliance 15_clinical_trial_change 16_work_rules_change 17_credit_traceability 18_spec_inspection_quality 19_consent_extraction

# 新ユースケース（提案評価、M&A DD）のみ実行
python scripts/run_cli_scenarios.py --mode dummy --ids 20_proposal_evaluation 21_ma_dd_integration

# フェーズだけ実行（create/execute/export/all）
python scripts/run_cli_scenarios.py --mode dummy --phase create

# 出力先を明示
python scripts/run_cli_scenarios.py --mode dummy --out data/usecase/cli_scenarios_custom
```

---

## シナリオ一覧

| ID | カテゴリ | ドメイン | ユースケース |
|----|----------|----------|--------------|
| 01 | 基本 | セキュリティ | 準拠評価 |
| 02 | 基本 | セキュリティ | ギャップ分析 |
| 03 | 基本 | 契約 | 差分・改訂影響 |
| 04 | 基本 | 開発 | トレーサビリティ |
| 05 | 基本 | 開示資料 | ドラフト作成 |
| 06 | 基本 | 契約 | 抽出・構造化 |
| 07 | 基本 | 規程 | 品質点検 |
| 08 | 基本 | インシデント | 要約 |
| 09 | 基本 | 製品 | Q&A |
| 10 | 基本 | 通知 | 翻訳 |
| 11 | 拡張 | 医療 | 準拠評価 |
| 12 | 拡張 | 金融AML | 準拠評価 |
| 13 | 拡張 | 人事労務 | ギャップ分析 |
| 14 | 拡張 | 製造品質 | 準拠評価 |
| 15 | 拡張 | 医療（治験） | 差分分析 |
| 16 | 拡張 | 人事労務 | 差分分析 |
| 17 | 拡張 | 金融（与信） | トレーサビリティ |
| 18 | 拡張 | 製造品質 | 品質点検 |
| 19 | 拡張 | 医療（同意書） | 抽出 |
| 20 | 新UC | 入札・調達 | 提案評価・ベンダー比較 |
| 21 | 新UC | M&A | DD統合分析 |
