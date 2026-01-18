# %% #import
import os
import re
import json
import asyncio
import dotenv
import threading
from datetime import datetime
import importlib

from typing import Any, Callable, Optional, Dict
from pydantic import BaseModel, Field, field_validator

from langchain_core.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import AzureChatOpenAI, ChatOpenAI
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware, TodoListMiddleware, ToolRetryMiddleware, ToolCallLimitMiddleware, ContextEditingMiddleware, ClearToolUsesEdit
from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data

import src.utils as utils
import src.ast_compare as ast_compare
import src.tools as tools
import src.schema as schema
import src.prompt as prompt
import src.agent_log_analyzer as agent_log_analyzer
import src.blueprint_ast_builder as blueprint_ast_builder
import src.ast_llm_summarizer as ast_llm_summarizer
import src.pdf_to_text_llm as pdf_to_text_llm

importlib.reload(utils)
importlib.reload(ast_compare)
importlib.reload(tools)
importlib.reload(schema)
importlib.reload(prompt)
importlib.reload(agent_log_analyzer)
importlib.reload(blueprint_ast_builder)
importlib.reload(ast_llm_summarizer)
importlib.reload(pdf_to_text_llm)

from src.utils import build_llm, convert_pdf_to_txt, print_message_logs, extract_message_logs, DebugLoggingMiddleware, show_all_chunks_by_level
from src.tools import extract_regex_matches, read_text_segment, get_file_length, read_text_file, compare_setup, compare_all_chunk_similarity_matching, compare_get_grouping, search_by_keyphrase, compare_get_chunk, compare_specified_chunks_diff, compare_specified_chunks_llm, read_ast, COMPARE_STATE, analyze_visual_contents, preview_blueprint_headings, validate_blueprint
from src.schema import DocumentAST,AgentResult,DocumentStructureBlueprint, PreAnalysisResult
from src.prompt import blueprint_ast_builder_prompt, blueprint_validate_prompt, compare_type_analysis_prompt, compare_parent_agent_prompt, compare_sub_agent_general, compare_sub_agent1, compare_sub_agent2, compare_sub_agent3, compare_sub_agent_report
from src.agent_log_analyzer import analyze_agent_log
from src.pdf_to_text_llm import convert_pdf_with_llm

import src.blueprint_ast_builder as blueprint_ast_builder
import src.ast_llm_summarizer as ast_llm_summarizer

importlib.reload(blueprint_ast_builder)
importlib.reload(ast_llm_summarizer)

# %% #初期設定
## 環境変数の読み込み
dotenv.load_dotenv()

## LLMクライアントの初期化
llm = build_llm()
llm_complex = build_llm(model="gpt-5.2")

# %% #blueprint作成の準備
## ツールの定義
tools_blueprint_builder = [read_text_segment, read_text_file, extract_regex_matches, get_file_length, analyze_visual_contents] 
tools_blueprint_validator = [read_text_segment, read_text_file, extract_regex_matches, get_file_length, preview_blueprint_headings, validate_blueprint, analyze_visual_contents] 

## ミドルウェアの定義
middleware_blueprint_builder = [
    DebugLoggingMiddleware(
        log_file="agent_debug_blueprint_builder.jsonl",
        overwrite=True,
        include_full_messages=False,
    ),
]
## blueprint作成エージェントの初期化
agent_blueprint_builder = create_deep_agent(
    model=llm_complex,
    tools=tools_blueprint_builder,
    system_prompt=blueprint_ast_builder_prompt,
    response_format=DocumentStructureBlueprint,
    middleware=middleware_blueprint_builder,
    subagents=[
    {
        "name": "validate_blueprint_agent",
        "description": "blueprintを複数の観点で検証して必要に応じて修正します。blueprintのパスを指示してください。", 
        "system_prompt": blueprint_validate_prompt,
        "tools": tools_blueprint_validator,
        "middleware": [
            DebugLoggingMiddleware(
                log_file="agent_debug_blueprint_builder.jsonl",
                overwrite=False,
                include_full_messages=False,
                is_subagent=True,
            ),
        ],
        "model": llm,
    },
    ],
    debug=False,
)

## ファイル情報の設定
target_file = "富士フィルム_統合報告書.pdf"
root_title = target_file.replace(".txt", "").replace(".pdf", "")
output_file = f"{root_title}_blueprint.json"
text_file = f"{root_title}.txt"

## ファイルが存在するか確認
print(f"target_file: {os.path.join('data', 'input', target_file)}")
if not os.path.exists(os.path.join("data", "input", target_file)):
    print(f"ファイルが存在しません: {os.path.join('data', 'input', target_file)}")
    exit()

## PDFをtxtに変換

### テキスト中心のPDF
# convert_pdf_to_txt(target_file)

### 視覚的な要素が多いPDF
result = asyncio.run(convert_pdf_with_llm(
    pdf_path=target_file,
    start_page=1,
    end_page=None,
    batch_size=20,
    use_image=True,
    verbose=False,
))
print(result)

# %% #blueprint作成
text_file = "富士フィルム_統合報告書.txt"

## ファイル内容を読み込む
with open(os.path.join("data", "input", text_file), "r", encoding="utf-8", errors="replace") as f:
    target = f.read()

## 仮想ファイルシステムを設定
initial_files_blueprint_builder = {
    f"/{text_file}": create_file_data(target)
}
## promptの設定
query_blueprint_builder = (
    f"ファイル '{text_file}' を解析してください。 "
    f"文書内のすべての見出しパターンを自律的に検出して、抽出するためのblueprintを生成してください。"
)
## 実行パラメータの設定
inputs_blueprint_builder = {
    "messages": [{"role": "user", "content": query_blueprint_builder}],
    "files": initial_files_blueprint_builder, 
}
## エージェントの実行
result_blueprint_builder = agent_blueprint_builder.invoke(inputs_blueprint_builder)
## ログの出力
print_message_logs(extract_message_logs(result_blueprint_builder))

## blueprintとログを取得
report = analyze_agent_log(
    log_file="agent_debug_blueprint_builder.jsonl",
    agent_result=result_blueprint_builder,
)
print(report)
blueprint = result_blueprint_builder.get("structured_response")

## blueprintの出力
with open(os.path.join("data", "blueprint", output_file), "w", encoding="utf-8") as f:
    json.dump(blueprint.model_dump(), f, ensure_ascii=False, indent=4)

# %% #AST構築
## AST構築
ast_path = os.path.join("data", "ast", f"{text_file}.ast.json") 
ast = blueprint_ast_builder.build_ast_from_blueprint(
    blueprint=blueprint,
    text_path=os.path.join("data", "input", text_file),
    out_ast_path=ast_path,
    root_title=root_title,
    max_content_chars_per_node=2000,
)
print(f"ASTを {ast_path} に出力しました。")

## ASTにcontent_summaryを付与
# ast_llm_summarizer.summarize_ast_inplace(ast_path=ast_path)
# print("content_summary を更新しました")

# %% #比較種別判定の準備
## ツールの定義
tools_compare_type_analysis = [read_ast, compare_get_grouping, search_by_keyphrase, compare_get_chunk, compare_specified_chunks_diff, compare_specified_chunks_llm, analyze_visual_contents]

## ミドルウェアの定義
middleware = [
    DebugLoggingMiddleware(
        log_file="agent_debug_compare_type_analysis.jsonl",
        overwrite=True,
        include_full_messages=False,
    ),
    ToolRetryMiddleware(
            max_retries=3,
            backoff_factor=2.0,
            initial_delay=1.0,
        ),
    ContextEditingMiddleware(
            edits=[
                ClearToolUsesEdit(
                    trigger=50000,
                    keep=3,
                ),
            ],
    )
]

## エージェントの初期化
agent_compare_type_analysis = create_deep_agent(
    model=llm_complex,
    tools=tools_compare_type_analysis,
    response_format=PreAnalysisResult,
    system_prompt=compare_type_analysis_prompt,
    middleware=middleware,
    debug=False,
)

# ## ファイル情報の設定
# docA = "富士フィルム_有価証券報告書.txt.ast.json"
# docB = "富士フィルム_有価証券報告書2.txt.ast.json"

# ## setupの実行
# _setup_json = compare_setup.invoke(
#     {
#         "docA": os.path.join("data", "ast", docA),
#         "docB": os.path.join("data", "ast", docB),
#         "embedding_model": None,
#         "cache_path": os.path.join("data", "embedding", "embedding_cache.json"),
#         "batch_size": 64,
#     }
# )

# ## all-chunk matchingの実行
# _initial_matching_json = compare_all_chunk_similarity_matching.invoke({"top_k": 3, "alpha": 0.3, "beta": 0.4, "min_score": 0.25})

# # 参照用に保存
# try:
#     COMPARE_STATE["initial_matching"] = json.loads(_initial_matching_json)
# except Exception:
#     COMPARE_STATE["initial_matching"] = None

# try:
#     _setup_obj = json.loads(_setup_json)
# except Exception:
#     _setup_obj = {"ok": False}

# print("compare agent ready. tools:", [t.name for t in tools_compare_type_analysis])
# print(
#     "warmup done:",
#     {
#         "docA": docA,
#         "docB": docB,
#         "setup_ok": bool(_setup_obj.get("ok")),
#         "initial_groups": (
#             len(COMPARE_STATE.get("initial_matching", {}).get("groups", []))
#             if isinstance(COMPARE_STATE.get("initial_matching"), dict)
#             else None
#         ),
#     },
# )
# show_all_chunks_by_level(COMPARE_STATE)

# %% #比較種別判定の実行
## ファイル情報の設定
docA = "富士フィルム_有価証券報告書.txt"
docB = "富士フィルム_有価証券報告書2.txt"

## ASTファイルの読み込み
with open(os.path.join("data", "input", docA), "r", encoding="utf-8") as f:
    doc_a = f.read()
with open(os.path.join("data", "input", docB), "r", encoding="utf-8") as f:
    doc_b = f.read()
with open(os.path.join("data", "embedding", "embedding_cache.json"), "r", encoding="utf-8") as f:
    cache = f.read()

## 仮想ファイルシステムに設定
initial_files_compare_type_analysis = {
    f"/{docA}": create_file_data(doc_a),
    f"/{docB}": create_file_data(doc_b),
    "/.embedding_cache.json": create_file_data(cache),
}

## promptの設定
query_compare_type_analysis = f"""
次の2つの文書の関係性を分析してください。また重点比較観点を分析するための具体的なプランを策定してください。

- docA: {docA}
- docB: {docB}

**重点比較観点**
- 変更箇所とその影響の特定

プランは概要レベルではなく、具体的かつ単純なタスクまで細分化・具体化して回答してください。汎用的なプランではなく、このタスクに特化した内容で問題ありません。
また網羅的に比較ができたことを確認するための検証ステップもプランには含めてください。その際に、今回の比較において何が確認できれば網羅性が担保できると言えるか考えて具体的に記載してください。

""".strip()

## 実行パラメータの設定
inputs_compare_type_analysis = {
    "messages": [{"role": "user", "content": query_compare_type_analysis}],
    "files": initial_files_compare_type_analysis,
}

## エージェントの実行
result_compare_type_analysis = agent_compare_type_analysis.invoke(inputs_compare_type_analysis)

## ログの出力
print_message_logs(extract_message_logs(result_compare_type_analysis))

##比較結果の取得
report_compare_type_analysis = analyze_agent_log(
    log_file="agent_debug_compare_type_analysis.jsonl",
    agent_result=result_compare_type_analysis,
)
print(report_compare_type_analysis)

# %% #比較分析の準備
## ツールの定義
tools_compare_analysis = [read_ast, compare_get_grouping, search_by_keyphrase, compare_get_chunk, compare_specified_chunks_diff, compare_specified_chunks_llm, analyze_visual_contents]

## ミドルウェアの設定
middleware_compare_analysis = [
    DebugLoggingMiddleware(
        log_file="agent_debug_compare_analysis.jsonl",
        overwrite=True,
        include_full_messages=False,
    ),
    ContextEditingMiddleware(
            edits=[
                ClearToolUsesEdit(
                    trigger=50000,
                    keep=3,
                ),
            ],
    )
]

agent_compare_analysis = create_deep_agent(
    model=llm_complex,
    system_prompt=compare_parent_agent_prompt,
    middleware=middleware_compare_analysis,
    subagents=[
        {
            "name": "compare_general_purpose_agent",
            "description": "ドキュメント比較に必要な準備作業や事前確認を汎用的に行うサブエージェントです。", 
            "system_prompt": compare_sub_agent_general,
            "tools": tools_compare_analysis,
            "middleware": [
                ToolCallLimitMiddleware(
                    tool_name="compare_specified_chunks_llm",
                    run_limit=5,
                    ),
                DebugLoggingMiddleware(
                    log_file="agent_debug_compare_analysis.jsonl",
                    overwrite=False,
                    include_full_messages=False,
                    is_subagent=True
                    )
            ],
            "model": llm,
        },
        {
            "name": "compare_agent",
            "description": "与えられた特定の観点でドキュメント間の比較を行います。ASTファイルの読み込みもできます。抽象的な依頼は時間がかかる傾向があるため、ステップバイステップで依頼をしてください。", 
            "system_prompt": compare_sub_agent1,
            "tools": tools_compare_analysis,
            "middleware": [
                ToolCallLimitMiddleware(
                    tool_name="compare_specified_chunks_llm",
                    run_limit=5,
                    ),
                DebugLoggingMiddleware(
                    log_file="agent_debug_compare_analysis.jsonl",
                    overwrite=False,
                    include_full_messages=False,
                    is_subagent=True
                    )
            ],
            "model": llm,
        },
        {
            "name": "deep_research_agent",
            "description": "compare_agentの結果に対して、より具体的な分析観点や論点について深掘りを行うサブエージェントです。", 
            "system_prompt": compare_sub_agent2,
            "tools": tools_compare_analysis,
            "middleware": [
                ToolCallLimitMiddleware(
                    tool_name="compare_specified_chunks_llm",
                    run_limit=5,
                    ),
                DebugLoggingMiddleware(
                    log_file="agent_debug_compare_analysis.jsonl",
                    overwrite=False,
                    include_full_messages=False,
                    is_subagent=True
                    )
            ],
            "model": llm,
        },
        {
            "name": "validate_agent",
            "description": "compare_agentやdeep_research_agentの結果に対して、特定の分析結果の妥当性を検証するサブエージェントです。", 
            "system_prompt": compare_sub_agent3,
            "tools": tools_compare_analysis,
            "middleware": [
                ToolCallLimitMiddleware(
                    tool_name="compare_specified_chunks_llm",
                    run_limit=5,
                    ),
                DebugLoggingMiddleware(
                    log_file="agent_debug_compare_analysis.jsonl",
                    overwrite=False,
                    include_full_messages=False,
                    is_subagent=True
                    )
            ],
            "model": llm,
        },
        {
            "name": "report_agent",
            "description": "分析結果をまとめて報告するサブエージェントです。形式やファイル出力の指定がある場合は具体的に指示をしてください。", 
            "system_prompt": compare_sub_agent_report,
            "model": llm,
            "middleware": [
                DebugLoggingMiddleware(
                    log_file="agent_debug_compare_analysis.jsonl",
                    overwrite=False,
                    include_full_messages=False,
                    is_subagent=True
                    )
            ],
        }
    ],
    debug=False
)

# %% #比較分析の実行
template_compare_analysis = "diff_analysis_template_fujifilm_yuho.md"

## ファイル内容を読み込む
with open(os.path.join("data", "input", docA), "r", encoding="utf-8") as f:
    doc_a = f.read()
with open(os.path.join("data", "input", docB), "r", encoding="utf-8") as f:
    doc_b = f.read()
with open(os.path.join("data", "embedding", "embedding_cache.json"), "r", encoding="utf-8") as f:
    cache = f.read()

## 仮想ファイルシステムに設定
initial_files_compare_analysis = {
    f"/{docA}": create_file_data(doc_a),
    f"/{docB}": create_file_data(doc_b),
    "/.embedding_cache.json": create_file_data(cache),
    # f"/{template_compare_analysis}": create_file_data(template_compare_analysis_content),
}

## promptの設定
query_compare_analysis = f"""
次の2つの文書について分析し、日本語で報告してください。
 - docA: {docA}
 - docB: {docB}
# 分析プラン

"relation\":\"Revision\",\"reason\":\"両文書とも同一会社（富士フイルムホールディングス）・同一提出日（2025年6月25日）・同一事業年度（第129期）・同一種別（有価証券報告書）であり、文書の「版違い（抽出/整形/一部修正）」とみなすのが自然です。一方で、冒頭の体裁が異なり（docAは「EDINET提出書類」等のヘッダが付く、docBは表紙ブロックから開始）、さらに同一箇所と思われる主要指標の表で数値の取り扱い/表現が異なる兆候（例: docAの売上高 3,195,828 に対し docB側では 2,195,828 と読める箇所があり、単なる表記ゆれでは説明できない可能性）があるため、「微修正(Fix)」ではなく、内容改訂を含む「改訂(Revision)」として差分抽出計画を立てるべきです。\",\"plan\":[\"【準備】比較の前提を固定する（作業ログに残す）\",\"1) docA/docBのファイル先頭200行を再取得し、(a)提出日、(b)事業年度、(c)会社名、(d)ページ総数表記（例: 1/225）をメモする。\",\"2) 以降の比較では、表記差（空白、全角/半角、英社名スペース有無、EDINETヘッダの有無）は“体裁差分”として扱い、重要差分（意味・数値・義務開示）から分離する方針を宣言する。\",\"3) diff記入先としてテンプレート（/diff_report_template_fujifilm_yuho.md）を複製し、作業用レポートファイル（例: /diff_report_fujifilm_yuho_filled.md）を作成してそこに記録する。\",\"【ステップA：章立てマッピング（網羅性の土台作り）】\",\"4) docAで「第○部」「【○○】」「○【○○】」「(1)(2)…」などの“章境界っぽい行”をgrepで抽出し、出現順に章リストを作る（行番号も控える）。\",\"5) docBでも同様に章境界っぽい行をgrep抽出して章リスト化する。\",\"6) 4)と5)を突き合わせ、まずは上位（第一部、第二部…相当）→中位（企業の概況、事業の内容…）→下位（主要指標、リスク…）の順で対応表を作る。対応が曖昧な場合は“前後200〜400行”を追加で読み、本文のトピックで確定させる。\",\"7) 対応表で「AにあってBにない」「BにあってAにない」を必ず棚卸しし、(a)本当に欠落、(b)別の場所へ移動、(c)体裁由来（重複ヘッダ等）のどれかに分類する。\",\"【ステップB：差分候補の自動抽出（変更箇所を漏れなく拾う）】\",\"8) まず“数値差分が起きやすい領域”を優先するため、両文書に対して以下のキーフレーズでgrep（content表示）し、該当箇所の行番号を収集する：\",\"  - 「主要な経営指標」「売上高」「税金等調整前」「キャッシュ・フロー」「総資産」「純資産」「従業員数」\",\"  - 「セグメント」「事業区分」「注」「(注)」\",\"9) 次に“影響が大きい開示領域”を拾うため、以下を両文書でgrepし、行番号を収集する：\",\"  - 会計： 「米国会計基準」「収益認識」「減損」「のれん」「引当金」「リース」「時価」「重要な会計上の見積り」\",\"  - リスク： 「事業等のリスク」「訴訟」「コンプライアンス」「サイバー」「情報セキュリティ」「災害」「地政学」\",\"  - ガバナンス： 「内部統制」「政策保有株式」「役員報酬」「取締役」「監査」「株主還元」「配当」\",\"10) 上記8)-9)の各ヒットについて、docA側・docB側で“同一トピックの近傍”をそれぞれ±150〜300行読み、ペア（比較単位）を作る（例：主要指標(連結)の表ブロック、事業の内容、セグメント変更の説明段落、等）。\",\"【ステップC：直接diffで変更箇所を確定（変更点→影響へ接続）】\",\"11) ペアごとに、まずはunified diff相当の比較を行う（手作業なら差分ツール、LLM比較なら「段落A vs 段落B」を投入）。\",\"  - ここでの目的は『差があるか/ないか』と『差分の正確なbefore/after』の確定。\",\"12) 差分を次の型にタグ付けしてテンプレ表へ起票する：\",\"  - 追加 / 削除 / 修正（意味変化あり） / 修正（意味変化なし＝体裁） / 移動 / 数値更新 / 範囲・定義変更\",\"13) “数値が違う”差分は必ず追加手順を踏む：\",\"  - (a) その数値が「同一項目・同一単位・同一対象範囲」か（百万円/千円、連結/提出会社、株式分割考慮前後など）を周辺の(注)で確認\",\"  - (b) 連鎖参照（同じ数値が別表にも出るか）を両文書内検索し、整合している方/崩れている方を把握\",\"  - (c) 影響欄には『投資家の理解/指標解釈が変わるか』『監査上の重要性があるか』を明記\",\"14) “セグメント名変更/区分変更”の差分は、説明段落（事業の内容等）と、セグメント情報（財務注記側）の両方で確認し、片側だけの差分になっていないかをチェックする（影響：前年比較可能性、開示の整合性）。\",\"15) “会計方針・見積り”の差分は、影響評価を必ず付ける（例：収益認識の判断基準変更→売上計上タイミングの変化可能性、減損兆候の記載追加→将来損失リスクの示唆、など）。\",\"【ステップD：変更点の影響評価（重点観点の中核）】\",\"16) 起票済みの変更点（CHG-ID）を、影響分類（財務/リスク/ガバナンス/その他）に振り分ける。\",\"17) 重要度判定ルールをこの案件用に固定し、各変更点に適用する：\",\"  - High: 数値・範囲定義・会計方針・リスク開示・法定記載（義務開示）に関する差分、または他章へ波及する差分\",\"  - Mid: 追加説明で解釈が変わり得るが数値自体は変わらない差分\",\"  - Low: 表記/空白/改行/並び替え等で意味不変\",\"18) Highについては必ず「フォローアップ」を書く（例：どの表/注記/MD&Aで裏取りするか、前年数値の再掲整合性、等）。\",\"【網羅性の検証ステップ（今回の比較で“何が確認できれば網羅的と言えるか”を明文化して実施）】\",\"19) 構造カバレッジ検証：ステップAで作った章対応表について、(a)docA主要章が全て“対応 or 未対応理由”になっている、(b)docB主要章も同様、を満たすまで繰り返す。これが満たせない限り網羅比較完了としない。\",\"20) 差分抽出カバレッジ検証：主要章ごとに「差分なし」宣言を含めた結論行を必ず置く（＝章単位で未評価をゼロにする）。\",\"21) 数値網羅性検証：少なくとも以下の“数値密集領域”について、差分表（テンプレ3章）に記録がある状態にする：\",\"  - 主要な経営指標（連結・提出会社）\",\"  - 株式/配当/株式分割に関する注記（分母・EPS等に影響）\",\"  - セグメント区分に関する説明（本文側）\",\"  ※本回答時点では全文走査は未実施のため、実作業では上記3つを最低ラインとして必ず消化する。\",\"22) 取りこぼし検証（ランダム再点検）：対応表で定義した主要章から各3箇所ずつランダムに行範囲を取り、再度A/Bを並べて確認し、新規差分が出ないことを確認する。新規差分が出た場合は、その章の比較粒度（ブロック分割）を細かくしてステップB〜Cをやり直す。\",\"23) キーフレーズ再検索検証：ステップ8)-9)のキーフレーズを“両文書で再grep”し、ヒットした全箇所が(1)差分表に起票済み、または(2)差分なしとして根拠行番号が記録済み、のどちらかになっていることを確認する。\",\"【成果物化】\",\"24) テンプレートに、(a)章対応表、(b)変更点一覧（影響・重要度付き）、(c)数値差分表、(d)網羅性チェックのチェック結果、を埋めて完成させる。\"],

""".strip()

inputs_compare_analysis = {
    "messages": [{"role": "user", "content": query_compare_analysis}],
    "files": initial_files_compare_analysis, 
}

## エージェントの実行
result_compare_analysis = agent_compare_analysis.invoke(inputs_compare_analysis)

## ログの出力
print_message_logs(extract_message_logs(result_compare_analysis))

## 比較結果の取得
report_compare_analysis = analyze_agent_log(
    log_file="agent_debug_compare_analysis.jsonl",
    agent_result=result_compare_analysis,
)
print(report_compare_analysis)


# %%
from langchain.tools import tool
from langchain_experimental.utilities import PythonREPL

python_repl = PythonREPL()

@tool
def python_repl_tool(code: str) -> str:
    """A Python shell. Use this to execute python commands."""
    return python_repl.run(code)

tools = [python_repl_tool]
print(tools)
# %%
code = """
import os
with open(os.path.join('data', 'input', '仕訳定義書.txt'), 'r', encoding='utf-8') as f:
    doc_a = f.read()
with open(os.path.join('data', 'input', '仕訳定義書.txt'), 'a', encoding='utf-8') as f:
    f.write('test')
"""

result = python_repl_tool.invoke(code)
print(result)


# %%
