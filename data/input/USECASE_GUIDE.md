## ユースケース別データ対応と期待値ガイド

このガイドは `design/usecase.md` の主要ユースケースに対し、`data/input/` のどの文書を使うかと、想定される出力の期待値（観点）を整理したものです。
すべて架空データです。

---

# 基本ユースケース（10種）

## 1) 準拠評価（Compliance / Conformance）

**データ（セキュリティドメイン）**
- 基準: `standard_security_baseline.txt`
- 対象: `policy_it_security.txt`
- 補助: `procedure_access_request.txt`, `procedure_vendor_onboarding.txt`, `procedure_key_management.txt`

**期待値**
- 未整備/要改善として拾われやすい項目: 監査ログの保全期間、MFA必須、再委託承認、鍵管理のKMS化、例外の期限管理。
- 根拠は「基準の該当条文」と「社内規程/手順の不足箇所」の双方から引用される。

**追加データ（医療ドメイン）**
- 基準: `healthcare_regulation_baseline.txt`
- 対象: `hospital_it_policy.txt`

**期待値（医療）**
- 未整備項目: 二要素認証未導入、監査ログ保存期間不足（3年vs5年）、委託先監査未実施、患者情報暗号化未対応
- 根拠: MED-01〜MED-10の各要求と病院規程の差分

**追加データ（金融AMLドメイン）**
- 基準: `aml_regulation_baseline.txt`
- 対象: `bank_aml_policy.txt`

**期待値（金融AML）**
- 未整備項目: リスク評価の頻度不足、PEPsスクリーニング未導入、記録保存期間不足、研修頻度不足
- 根拠: AML-01〜AML-10の各要求と銀行規程の差分

**追加データ（製造QMSドメイン）**
- 基準: `iso9001_requirements.txt`
- 対象: `manufacturing_qms_manual.txt`

**期待値（製造QMS）**
- ISO 9001要求事項への適合状況
- 改善機会の特定

---

## 2) ギャップ分析（Gap / Missing / Coverage）

**データ**
- 基準: `standard_security_baseline.txt`
- 対象: `policy_it_security.txt`
- 補助: `procedure_access_request.txt`, `procedure_vendor_onboarding.txt`, `procedure_key_management.txt`

**期待値**
- 「未対応/部分対応/対応済み」の区分が出る。
- 例: 監査ログの保存期間が365日未満、再委託の承認フロー不在、共有フォルダで鍵保管などがギャップとして列挙される。

**追加データ（人事労務ドメイン）**
- 基準: `labor_law_baseline.txt`
- 対象: `company_work_rules_v1.txt`

**期待値（人事労務）**
- 法定要件との差分: 育児休業規定の不足、ハラスメント相談窓口の未設置、割増賃金率（60時間超）の未対応

---

## 3) 差分・改訂影響分析（Redline / Change Impact）

**データ（契約）**
- 旧: `contract_service_v1.txt`
- 新: `contract_service_v2.txt`
- 追加: `contract_service_v3.txt`

**期待値**
- v1→v2、v2→v3 で「SLO」「通知期限」「ログ保全」「再委託」「違約金」などの変更点が分類される。
- 影響として「運用手順/契約条件/監査対応の更新」候補が示される。

**追加データ（治験プロトコル）**
- 旧: `clinical_trial_protocol_v1.txt`
- 新: `clinical_trial_protocol_v2.txt`

**期待値（治験）**
- 変更点: 用量漸増デザイン変更、対象患者基準変更、サイクル期間短縮、症例数増加、バイオマーカー評価追加
- 影響: 治験届の変更、IRB再審査、被験者への再同意取得要否

**追加データ（就業規則）**
- 旧: `company_work_rules_v1.txt`
- 新: `company_work_rules_v2.txt`

**期待値（就業規則）**
- 変更点: 試用期間延長、フレックス・テレワーク導入、割増賃金率変更、再雇用年齢引上げ、副業解禁
- 影響: 従業員への周知、労基署届出、雇用契約書改定

---

## 4) トレーサビリティ（Traceability / Mapping）

**データ（プロダクト開発）**
- 要求: `requirements_product_x.txt`
- 設計: `design_product_x.txt`
- テスト: `test_spec_product_x.txt`

**期待値**
- 要求→設計→テストの対応表（または対応候補一覧）が出る。
- 例: MFA必須・ログ保持365日・稼働率99.9%・削除30日などが、設計/テストで未達・不足として示される。

**追加データ（与信審査システム）**
- 要求: `credit_scoring_requirements.txt`
- 設計: `credit_scoring_design.txt`
- テスト: `credit_scoring_test_spec.txt`

**期待値（与信審査）**
- REQ-F01〜F07、REQ-N01〜N05の各要件に対する設計・テストの対応状況
- 欠落/部分対応の特定（例: 公平性テストの網羅性）

---

## 5) 生成・ドラフト作成（Synthesis / Drafting）

**データ**
- 入力: `company_profile.txt`, `product_overview_x.txt`, `risk_register_q4.txt`
- テンプレ: `template_disclosure_draft.md`

**期待値**
- テンプレの各セクションに、会社概要・製品概要・統制・リスクが記入される。
- リスク登録簿の改善案が「改善計画」に反映される。

---

## 6) 抽出・構造化（Extraction / Normalization）

**データ**
- 入力: `privacy_addendum_sample.txt`

**期待値**
- 期限（72時間/24時間/30日/365日）、義務（事前承認・KMS・ローテーション）、禁止（平文保存）などが抽出される。
- JSONや表形式での抽出テンプレに落としやすい構造。

**追加データ（同意書）**
- 入力: `patient_consent_form.txt`

**期待値（同意書）**
- 抽出対象: 研究目的、予想されるリスク、予想される利益、費用負担、補償条件、連絡先
- 構造化出力でレビュー効率化

---

## 7) 品質点検（Quality / Consistency）

**データ**
- 単一文書: `single_policy_with_issues.txt`
- 用語集: `glossary_terms.txt`

**期待値**
- 共有IDの扱い、権限付与の承認不備、ログ未取得、例外期限なし、参照文書の版不一致などが指摘される。
- 用語ゆれ（重要情報/機密情報/監査ログの定義）が検出される。

**追加データ（製品仕様 vs 検査手順）**
- 仕様書: `product_spec_part_a.txt`
- 手順書: `inspection_procedure.txt`

**期待値（製造品質）**
- 仕様書と検査手順の整合性確認
- 検査項目の過不足、判定基準の不一致検出

---

## 8) 要約・ブリーフィング（Summarization）

**データ**
- 入力: `incident_postmortem_2026_01.txt`

**期待値**
- 経営向け/現場向け/監査向けなどの要点整理が可能。
- 影響・原因・再発防止・意思決定が整理される。

---

## 9) 検索・Q&A（Evidence-backed QA）

**データ**
- 文書群: `faq_product_x.txt`, `policy_it_security.txt`, `contract_service_v2.txt`
- 質問: `questions_product_x.txt`

**期待値**
- 質問ごとに「回答 + 根拠」の形式で返る。
- 根拠はFAQや契約書の該当条文から引用される。

---

## 10) 多言語・表現変換（Translation / Rewriting）

**データ**
- 入力: `jp_security_notice.txt`

**期待値**
- 英訳または平易化（社内向け）に変換できる。
- 重要な事実（原因/影響/対策）が保持される。

---

# 拡張ユースケース

## 11) 提案評価・ベンダー比較（Proposal Evaluation）

**データ**
- RFP: `rfp_cloud_system.txt`
- 提案A: `proposal_vendor_a.txt`
- 提案B: `proposal_vendor_b.txt`
- 評価基準: `proposal_evaluation_criteria.txt`

**期待値**
- RFP要件への適合度評価（各ベンダー）
- ベンダー間の比較表（技術提案、費用、体制、SLA等）
- 評価基準に基づくスコアリング支援
- 推奨ベンダーと根拠の提示

**応用例**
- システム導入ベンダー選定
- 業務委託先の選定
- 製品・サービスの比較評価

---

## 12) M&A デューデリジェンス（Due Diligence）

**データ**
- 対象会社概要: `ma_target_company_profile.txt`
- 法務DDチェックリスト: `ma_legal_dd_checklist.txt`
- 法務DD報告書: `ma_legal_dd_findings.txt`
- 財務DD報告書: `ma_financial_dd_findings.txt`

**期待値**
- 法務・財務DDの発見事項の統合分析
- リスク項目の優先度付け
- 価格調整・表明保証条項への反映事項
- クロージング前/後の対応事項リスト

**応用例**
- M&A/投資案件のリスク評価
- PMI（統合後）の課題特定
- 契約交渉の論点整理

---

# ドメイン別データ一覧

## セキュリティ・ガバナンス
- `standard_security_baseline.txt` - 情報セキュリティ基準
- `policy_it_security.txt` - 社内セキュリティ規程
- `procedure_*.txt` - 各種手順書
- `contract_service_v1/v2/v3.txt` - クラウドサービス契約
- `privacy_addendum_sample.txt` - 個人情報取扱付属契約

## 医療・ヘルスケア
- `healthcare_regulation_baseline.txt` - 医療情報システム安全管理ガイドライン
- `hospital_it_policy.txt` - 病院情報セキュリティ規程
- `clinical_trial_protocol_v1/v2.txt` - 治験実施計画書（版比較用）
- `patient_consent_form.txt` - 同意説明文書

## 金融・銀行
- `aml_regulation_baseline.txt` - マネーローンダリング対策ガイドライン
- `bank_aml_policy.txt` - 銀行AML規程
- `credit_scoring_requirements.txt` - 与信審査システム要件定義
- `credit_scoring_design.txt` - 与信審査システム設計書
- `credit_scoring_test_spec.txt` - 与信審査システムテスト仕様

## 人事・労務
- `labor_law_baseline.txt` - 就業規則に関する法的要件
- `company_work_rules_v1/v2.txt` - 就業規則（版比較用）
- `performance_review_guideline.txt` - 人事評価制度ガイドライン

## 製造・品質管理
- `iso9001_requirements.txt` - ISO 9001要求事項
- `manufacturing_qms_manual.txt` - 品質マニュアル
- `product_spec_part_a.txt` - 製品仕様書
- `inspection_procedure.txt` - 検査手順書

## 入札・調達
- `rfp_cloud_system.txt` - 提案依頼書（RFP）
- `proposal_vendor_a.txt` - ベンダーA提案書
- `proposal_vendor_b.txt` - ベンダーB提案書
- `proposal_evaluation_criteria.txt` - 提案評価基準

## M&A・デューデリジェンス
- `ma_target_company_profile.txt` - 対象会社概要
- `ma_legal_dd_checklist.txt` - 法務DDチェックリスト
- `ma_legal_dd_findings.txt` - 法務DD調査報告書
- `ma_financial_dd_findings.txt` - 財務DD調査報告書
