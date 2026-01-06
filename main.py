# %% #import
import os
import re
import json
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
from src.tools import extract_regex_matches, read_text_segment, get_file_length, read_text_file, compare_setup, compare_all_chunk_similarity_matching, compare_get_grouping, compare_search_by_keyphrase, compare_get_chunk, compare_specified_chunks_diff, compare_specified_chunks_llm, read_ast, COMPARE_STATE, analyze_visual_contents, preview_blueprint_headings, validate_blueprint
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
result = await convert_pdf_with_llm(  # type: ignore[top-level-await]
    pdf_path=target_file,
    start_page=1,
    end_page=None,
    batch_size=20,
    use_image=True,
    verbose=False,
)
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
tools_compare_type_analysis = [read_ast, compare_get_grouping, compare_search_by_keyphrase, compare_get_chunk, compare_specified_chunks_diff, compare_specified_chunks_llm, analyze_visual_contents]

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

## ファイル情報の設定
docA = "富士フィルム_有価証券報告書.txt.ast.json"
docB = "富士フィルム_有価証券報告書2.txt.ast.json"

## setupの実行
_setup_json = compare_setup.invoke(
    {
        "docA": os.path.join("data", "ast", docA),
        "docB": os.path.join("data", "ast", docB),
        "embedding_model": None,
        "cache_path": os.path.join("data", "embedding", "embedding_cache.json"),
        "batch_size": 64,
    }
)

## all-chunk matchingの実行
_initial_matching_json = compare_all_chunk_similarity_matching.invoke({"top_k": 3, "alpha": 0.3, "beta": 0.4, "min_score": 0.25})

# 参照用に保存
try:
    COMPARE_STATE["initial_matching"] = json.loads(_initial_matching_json)
except Exception:
    COMPARE_STATE["initial_matching"] = None

try:
    _setup_obj = json.loads(_setup_json)
except Exception:
    _setup_obj = {"ok": False}

print("compare agent ready. tools:", [t.name for t in tools_compare_type_analysis])
print(
    "warmup done:",
    {
        "docA": docA,
        "docB": docB,
        "setup_ok": bool(_setup_obj.get("ok")),
        "initial_groups": (
            len(COMPARE_STATE.get("initial_matching", {}).get("groups", []))
            if isinstance(COMPARE_STATE.get("initial_matching"), dict)
            else None
        ),
    },
)
show_all_chunks_by_level(COMPARE_STATE)

# %% #比較種別判定の実行
## ファイル情報の設定
docA = "富士フィルム_有価証券報告書.txt.ast.json"
docB = "富士フィルム_有価証券報告書2.txt.ast.json"

## ASTファイルの読み込み
with open(os.path.join("data", "ast", docA), "r", encoding="utf-8") as f:
    ast_a = f.read()
with open(os.path.join("data", "ast", docB), "r", encoding="utf-8") as f:
    ast_b = f.read()
with open(os.path.join("data", "embedding", "embedding_cache.json"), "r", encoding="utf-8") as f:
    cache = f.read()

## 仮想ファイルシステムに設定
initial_files_compare_type_analysis = {
    f"/{docA}": create_file_data(ast_a),
    f"/{docB}": create_file_data(ast_b),
    "/.embedding_cache.json": create_file_data(cache),
}

## promptの設定
query_compare_type_analysis = f"""
次の2つのAST文書（*.ast.json）の関係性を分析してください。また重点比較観点を分析するための具体的なプランを策定してください。

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
tools_compare_analysis = [read_ast, compare_get_grouping, compare_search_by_keyphrase, compare_get_chunk, compare_specified_chunks_diff, compare_specified_chunks_llm, analyze_visual_contents]

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
with open(os.path.join("data", "ast", docA), "r", encoding="utf-8") as f:
    ast_a = f.read()
with open(os.path.join("data", "ast", docB), "r", encoding="utf-8") as f:
    ast_b = f.read()
with open(os.path.join("data", "embedding", "embedding_cache.json"), "r", encoding="utf-8") as f:
    cache = f.read()
with open(os.path.join("templates", template_compare_analysis), "r", encoding="utf-8") as f:
    template_compare_analysis_content = f.read()

## 仮想ファイルシステムに設定
initial_files_compare_analysis = {
    f"/{docA}": create_file_data(ast_a),
    f"/{docB}": create_file_data(ast_b),
    "/.embedding_cache.json": create_file_data(cache),
    f"/{template_compare_analysis}": create_file_data(template_compare_analysis_content),
}

## promptの設定
query_compare_analysis = f"""
次の2つの文書（*.ast.json）について分析し、日本語で報告してください。
 - docA: {docA}
 - docB: {docB}
# 分析観点
以下に記載されているテンプレートを埋めてください。テンプレートは最後に一括で更新せず、段階的に編集してください。
最後にチェックリストもあるので漏れなく確認して埋めてください。
チェックがつけられない場合は、なぜチェックできないか根拠を記載したうえで、代替となる観点でチェックを行ってください。
{template_compare_analysis}
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
