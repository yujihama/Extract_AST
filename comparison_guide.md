# 文書比較テンプレート 記載ガイド

## 1. 概要

このガイドは `comparison_template.json` の記載方法を説明します。

### 基本方針
- **docA（富士フイルム）を基準**として、docB（三菱ケミカル）の対応セクションをマッピング
- 2階層目（第１【企業の概況】レベル）を基本単位とする
- 子セクションが5件以上、または構造差が大きい場合は3階層目に展開

---

## 2. フィールド定義

### 2.1 `docB_match` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `status` | string | ✅ | マッピング状態（下記参照） |
| `chunk_id` | string | △ | docBの対応チャンクID（statusがmapped/partialの場合必須） |
| `title` | string | △ | docBの対応セクションタイトル |
| `similarity` | number | - | 類似度スコア（0.0〜1.0、算出した場合のみ） |
| `mapping_confidence` | string | - | マッピングの確信度（high/medium/low） |

#### `status` の値

| 値 | 意味 | 説明 |
|----|------|------|
| `mapped` | 完全マッピング | docBに同等のセクションが存在 |
| `partial` | 部分マッピング | docBに類似セクションがあるが、構造や範囲が異なる |
| `none` | マッピングなし | docBに対応セクションが存在しない |
| `multiple` | 複数マッピング | docBの複数セクションに分散している |

---

### 2.2 `comparison` オブジェクト

| フィールド | 型 | 必須 | 説明 |
|-----------|-----|------|------|
| `diff_type` | string | ✅ | 差分の種類（下記参照） |
| `noise_items` | array | - | ノイズ差分のリスト |
| `semantic_diffs` | array | - | 意味的差分のリスト |
| `key_diffs` | array | - | 主要な差分（childrenの場合） |
| `notes` | string | - | 補足事項 |

#### `diff_type` の値

| 値 | 意味 | 説明 |
|----|------|------|
| `identical` | 同一 | 意味的に同一（表記揺れ程度） |
| `noise_only` | ノイズのみ | 社名置換、改行、全角半角等のノイズ差のみ |
| `semantic_diff` | 意味差分あり | 数値、条件、定義等に実質的な差がある |
| `structural_diff` | 構造差分あり | セクション構成や階層が異なる |
| `not_applicable` | 該当なし | docBに対応セクションがない |

---

### 2.3 ノイズ vs 実差分の判定基準

#### ノイズとして扱うもの（diff_type: `noise_only`）
- 改行・行折返しの差
- 全角/半角・空白・句読点の差
- 数字表記のカンマ有無等（実数が同じなら）
- 社名・英訳名・EDINETコード等の固有名詞置換のみ
- 目次・ページ番号等のレイアウト差

#### 実差分として扱うもの（diff_type: `semantic_diff`）
- 数値の実質的な差異
- 条件・例外・範囲の差
- 定義の差（例：営業利益 vs コア営業利益）
- 義務/任意の差
- 「該当事項はありません」の有無
- KPI・指標の定義差
- 委員会・体制名の差（単なる名称ノイズか、体制差か判断要）

---

## 3. 記載手順

### Step 1: docBのAST構造を確認
`三菱ケミカル_有価証券報告書.txt.ast.json` を開き、2階層目のセクション一覧を把握する。

### Step 2: 各セクションをマッピング
テンプレートの各 `section_mapping` エントリに対して：

1. docBで対応するセクションを特定
2. `docB_match.status` を設定
3. `docB_match.chunk_id` と `docB_match.title` を記入
4. 類似度が算出可能なら `similarity` を記入

### Step 3: 差分を分析・記録
1. `comparison.diff_type` を判定
2. ノイズ差分があれば `noise_items` に記録
3. 意味的差分があれば `semantic_diffs` に記録

### Step 4: 展開セクションの子要素を処理
`expanded: true` のセクション（SEC-002）については、`children` 配列内の各エントリも同様に処理する。

### Step 5: docB未マッチセクションを記録
docAに対応がないdocB固有のセクションを `unmapped_docB_sections` に追記する。

### Step 6: サマリーを作成
`summary` セクションに集計結果と全体評価を記入する。

---

## 4. 記載例

### 4.1 mapped（完全マッピング）の例

```json
{
  "id": "SEC-001",
  "docA": {
    "chunk_id": "0/0/0",
    "title": "第１【企業の概況】",
    "depth": 2,
    "child_count": 5
  },
  "docB_match": {
    "status": "mapped",
    "chunk_id": "0/0/0",
    "title": "第１【企業の概況】",
    "similarity": 0.92,
    "mapping_confidence": "high"
  },
  "expanded": false,
  "comparison": {
    "diff_type": "noise_only",
    "noise_items": [
      "社名: 富士フイルム → 三菱ケミカル",
      "提出日: 2025年6月25日 → 2025年6月23日"
    ],
    "semantic_diffs": [],
    "notes": "基本構成は同一"
  }
}
```

### 4.2 semantic_diff（意味差分あり）の例

```json
{
  "id": "SEC-002-01",
  "docA": {
    "chunk_id": "0/0/1/0",
    "title": "１【経営方針、経営環境及び対処すべき課題等】",
    "depth": 3
  },
  "docB_match": {
    "status": "mapped",
    "chunk_id": "0/0/1/0",
    "title": "１【経営方針、経営環境及び対処すべき課題等】",
    "similarity": 0.65
  },
  "comparison": {
    "diff_type": "semantic_diff",
    "key_diffs": [
      {
        "topic": "中期経営計画名",
        "docA_value": "VISION2030",
        "docB_value": "KAITEKI Vision 35 / 新中期経営計画2029"
      },
      {
        "topic": "主要KPI定義",
        "docA_value": "営業利益、ROE、ROIC",
        "docB_value": "コア営業利益、EPS、ROE、ROIC"
      }
    ],
    "notes": "経営戦略の骨子は類似するが、計画名・KPI定義が異なる"
  }
}
```

### 4.3 none（マッピングなし）の例

```json
{
  "id": "SEC-XXX",
  "docA": {
    "chunk_id": "0/0/X/Y",
    "title": "【XX固有セクション】",
    "depth": 3
  },
  "docB_match": {
    "status": "none",
    "chunk_id": "",
    "title": "",
    "similarity": null
  },
  "comparison": {
    "diff_type": "not_applicable",
    "notes": "docBには該当セクションなし。富士フイルム固有の開示項目と推定。"
  }
}
```

### 4.4 unmapped_docB_sections の例

```json
{
  "id": "UNMAPPED-001",
  "chunk_id": "0/0/0/4/2",
  "title": "(3) 労働組合の状況",
  "title_path": ["第一部【企業情報】", "第１【企業の概況】", "５【従業員の状況】", "(3) 労働組合の状況"],
  "category": "労務",
  "possible_reason": "docAでは従業員の状況に労組情報が含まれていない",
  "notes": "三菱ケミカル固有の開示。docA側では該当記載なし。"
}
```

---

## 5. カテゴリ一覧（unmapped_docB_sections用）

| カテゴリ | 説明 |
|----------|------|
| `内部統制` | 内部統制報告書、監査報告関連 |
| `第二部` | 第二部【提出会社の保証会社等の情報】配下 |
| `労務` | 労働組合、人事制度固有 |
| `制度差` | 会計基準差（IFRS vs 米国会計基準）、開示制度差 |
| `構造差` | 同内容だがdocAでは別階層に配置 |
| `会社固有` | 会社特有の事業・体制に関する記載 |
| `その他` | 上記に該当しない |

---

## 6. 注意事項

### 6.1 展開判断の基準
以下の場合は `expanded: true` として子セクションを個別にマッピング：
- 子セクションが5件以上
- docAとdocBで子セクションの構成が大きく異なる
- 個別に意味差分を追跡する必要がある

### 6.2 会計基準の差異
- docA（富士フイルム）: **米国会計基準**
- docB（三菱ケミカル）: **IFRS**

この差異により、経理の状況（SEC-005）では構造・用語が異なる可能性が高い。

### 6.3 セグメント区分の差異
両社でセグメント区分が異なるため、セグメント別の数値比較は直接行えない。
- docA: ヘルスケア、エレクトロニクス、ビジネスイノベーション、イメージング
- docB: スペシャリティマテリアルズ、MMA&デリバティブズ、ベーシックマテリアルズ&ポリマーズ、ファーマ、産業ガス

---

## 7. 成果物チェックリスト

記載完了時に以下を確認：

- [ ] 全7セクション（SEC-001〜SEC-007）の `docB_match.status` が記入済み
- [ ] SEC-002の全6子セクション（SEC-002-01〜SEC-002-06）が記入済み
- [ ] `status` が `mapped` または `partial` の場合、`chunk_id` と `title` が記入済み
- [ ] 各セクションの `comparison.diff_type` が記入済み
- [ ] `semantic_diff` の場合、具体的な差分内容が記録済み
- [ ] docB固有セクションが `unmapped_docB_sections` に追記済み
- [ ] `summary` セクションの集計が完了

---

## 8. 参考：docAセクション一覧

| ID | chunk_id | タイトル | 子セクション数 | 展開 |
|----|----------|----------|---------------|------|
| SEC-001 | 0/0/0 | 第１【企業の概況】 | 5 | ❌ |
| SEC-002 | 0/0/1 | 第２【事業の状況】 | 6 | ✅ |
| SEC-003 | 0/0/2 | 第３【設備の状況】 | 3 | ❌ |
| SEC-004 | 0/0/3 | 第４【提出会社の状況】 | 4 | ❌ |
| SEC-005 | 0/0/4 | 第５【経理の状況】 | 2 | ❌ |
| SEC-006 | 0/0/5 | 第６【提出会社の株式事務の概要】 | 0 | ❌ |
| SEC-007 | 0/0/6 | 第７【提出会社の参考情報】 | 2 | ❌ |

---

## 9. 問い合わせ

不明点がある場合は、テンプレート作成者に確認してください。

