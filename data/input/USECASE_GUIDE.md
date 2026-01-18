## ユースケース別データ対応と期待値ガイド

このガイドは `design/usecase.md` の主要ユースケースに対し、`data/input/` のどの文書を使うかと、想定される出力の期待値（観点）を整理したものです。
すべて架空データです。

---

## 1) 準拠評価（Compliance / Conformance）

**データ**
- 基準: `standard_security_baseline.txt`
- 対象: `policy_it_security.txt`
- 補助: `procedure_access_request.txt`, `procedure_vendor_onboarding.txt`, `procedure_key_management.txt`

**期待値**
- 未整備/要改善として拾われやすい項目: 監査ログの保全期間、MFA必須、再委託承認、鍵管理のKMS化、例外の期限管理。
- 根拠は「基準の該当条文」と「社内規程/手順の不足箇所」の双方から引用される。

---

## 2) ギャップ分析（Gap / Missing / Coverage）

**データ**
- 基準: `standard_security_baseline.txt`
- 対象: `policy_it_security.txt`
- 補助: `procedure_access_request.txt`, `procedure_vendor_onboarding.txt`, `procedure_key_management.txt`

**期待値**
- 「未対応/部分対応/対応済み」の区分が出る。
- 例: 監査ログの保存期間が365日未満、再委託の承認フロー不在、共有フォルダで鍵保管などがギャップとして列挙される。

---

## 3) 差分・改訂影響分析（Redline / Change Impact）

**データ**
- 旧: `contract_service_v1.txt`
- 新: `contract_service_v2.txt`
- 追加: `contract_service_v3.txt`

**期待値**
- v1→v2、v2→v3 で「SLO」「通知期限」「ログ保全」「再委託」「違約金」などの変更点が分類される。
- 影響として「運用手順/契約条件/監査対応の更新」候補が示される。

---

## 4) トレーサビリティ（Traceability / Mapping）

**データ**
- 要求: `requirements_product_x.txt`
- 設計: `design_product_x.txt`
- テスト: `test_spec_product_x.txt`

**期待値**
- 要求→設計→テストの対応表（または対応候補一覧）が出る。
- 例: MFA必須・ログ保持365日・稼働率99.9%・削除30日などが、設計/テストで未達・不足として示される。

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

---

## 7) 品質点検（Quality / Consistency）

**データ**
- 単一文書: `single_policy_with_issues.txt`
- 用語集: `glossary_terms.txt`

**期待値**
- 共有IDの扱い、権限付与の承認不備、ログ未取得、例外期限なし、参照文書の版不一致などが指摘される。
- 用語ゆれ（重要情報/機密情報/監査ログの定義）が検出される。

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

