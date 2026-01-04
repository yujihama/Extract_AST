"""
agent_debug.jsonl ログの分析・可視化モジュール

使用方法（Jupyter Notebook）:
    from agent_log_analyzer import analyze_agent_log
    
    # エージェント実行後、結果とログを一括保存
    report = analyze_agent_log(
        log_file="agent_debug.jsonl",
        agent_result=result2  # エージェント実行結果（仮想FSを含む）
    )
    print(report)
"""

import json
import shutil
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional, Any

# ログディレクトリの定数
LOG_DIR = Path("log")


def load_jsonl(file_path: str) -> list[dict]:
    """JSONLファイルを読み込む"""
    records = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # 不正な行はスキップ
    return records


def parse_agent_logs(records: list[dict]) -> dict:
    """ログレコードを解析してエージェント/ツール情報を抽出"""
    
    agents = {}
    parent_map = {}
    tool_calls = defaultdict(list)
    subagent_instructions = []
    pending_task_calls = {}
    
    # 最終ステータスを追跡
    final_status = "unknown"
    has_error = False
    
    for record in records:
        event = record.get('event', '')
        invoke_id = record.get('invoke_id', '')
        timestamp = record.get('timestamp', '')
        
        if event == 'agent_start':
            last_message = record.get('last_message', [])
            first_msg = last_message[0] if last_message else {}
            agents[invoke_id] = {
                'invoke_id': invoke_id,
                'start_time': timestamp,
                'end_time': None,
                'initial_message': first_msg.get('content', '')[:300],
                'total_messages': record.get('message_count', 0),
                'is_subagent': False,
                'parent_invoke_id': None,
                'tool_calls': [],
            }
            
        elif event == 'agent_end':
            if invoke_id in agents:
                agents[invoke_id]['end_time'] = timestamp
                agents[invoke_id]['total_messages'] = record.get('total_messages', 0)
                agents[invoke_id]['final_response'] = record.get('final_response_preview', '')[:400]
            # メインエージェントの終了を検出
            if not agents.get(invoke_id, {}).get('is_subagent', True):
                final_status = "completed"
                
        elif event == 'agent_error':
            has_error = True
            final_status = "error"
                
        elif event == 'tool_call_start':
            tool_name = record.get('tool_name', '')
            tool_args = record.get('tool_args', {})
            tool_call_id = record.get('tool_call_id', '')
            
            tool_info = {
                'tool_name': tool_name,
                'tool_args': tool_args,
                'tool_call_id': tool_call_id,
                'start_time': timestamp,
                'result': None,
                'duration_ms': None,
                'status': None,
            }
            tool_calls[invoke_id].append(tool_info)
            
            if tool_name == 'task':
                subagent_type = tool_args.get('subagent_type', 'unknown')
                description = tool_args.get('description', '')
                pending_task_calls[tool_call_id] = {
                    'parent_invoke_id': invoke_id,
                    'subagent_type': subagent_type,
                    'description': description,
                    'tool_call_id': tool_call_id,
                    'child_invoke_id': None,
                }
                
        elif event == 'tool_call_result':
            tool_call_id = record.get('tool_call_id', '')
            status = record.get('status', 'unknown')
            if status == 'error':
                has_error = True
            for inv_id, calls in tool_calls.items():
                for call in calls:
                    if call['tool_call_id'] == tool_call_id:
                        call['result'] = record.get('result', record.get('result_preview', ''))[:500]
                        call['duration_ms'] = record.get('duration_ms')
                        call['status'] = status
                        break
    
    # サブエージェントの親子関係を推定
    sorted_records = sorted(records, key=lambda x: x.get('timestamp', ''))
    
    for i, record in enumerate(sorted_records):
        if record.get('event') == 'agent_start':
            child_invoke_id = record.get('invoke_id', '')
            
            for j in range(i - 1, max(0, i - 20), -1):
                prev = sorted_records[j]
                if prev.get('event') == 'tool_call_start' and prev.get('tool_name') == 'task':
                    parent_invoke_id = prev.get('invoke_id', '')
                    tool_call_id = prev.get('tool_call_id', '')
                    
                    if parent_invoke_id != child_invoke_id:
                        parent_map[child_invoke_id] = parent_invoke_id
                        
                        if child_invoke_id in agents:
                            agents[child_invoke_id]['is_subagent'] = True
                            agents[child_invoke_id]['parent_invoke_id'] = parent_invoke_id
                            
                        if tool_call_id in pending_task_calls:
                            pending_task_calls[tool_call_id]['child_invoke_id'] = child_invoke_id
                        break
    
    for invoke_id, calls in tool_calls.items():
        if invoke_id in agents:
            agents[invoke_id]['tool_calls'] = calls
            
    for tc in pending_task_calls.values():
        subagent_instructions.append(tc)
    
    # 最終ステータスの判定
    if has_error:
        final_status = "error"
    elif final_status == "unknown":
        # agent_endが見つからなかった場合
        if agents:
            final_status = "incomplete"
        else:
            final_status = "empty"
    
    return {
        'agents': agents,
        'parent_map': parent_map,
        'subagent_instructions': subagent_instructions,
        'final_status': final_status,
        'has_error': has_error,
    }


def generate_text_report(parsed_data: dict, log_file: str, run_folder: str) -> str:
    """テキスト形式のレポートを生成（改善版）"""
    
    agents = parsed_data['agents']
    subagent_instructions = parsed_data['subagent_instructions']
    final_status = parsed_data.get('final_status', 'unknown')
    
    # サブエージェント指示をinvoke_idでマッピング
    instruction_by_child = {}
    for inst in subagent_instructions:
        child_id = inst.get('child_invoke_id')
        if child_id:
            instruction_by_child[child_id] = inst
    
    lines = []
    lines.append("=" * 80)
    lines.append("Agent Debug Log Analysis Report")
    lines.append("=" * 80)
    lines.append(f"Log file: {log_file}")
    lines.append(f"Run folder: {run_folder}")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Final Status: {final_status}")
    lines.append("")
    
    # 統計サマリー
    total_agents = len(agents)
    main_agents = len([a for a in agents.values() if not a.get('is_subagent')])
    sub_agents = total_agents - main_agents
    total_tools = sum(len(a.get('tool_calls', [])) for a in agents.values())
    
    lines.append("-" * 80)
    lines.append("SUMMARY")
    lines.append("-" * 80)
    lines.append(f"Total Agents: {total_agents}")
    lines.append(f"  - Main Agents: {main_agents}")
    lines.append(f"  - Sub Agents: {sub_agents}")
    lines.append(f"Total Tool Calls: {total_tools}")
    lines.append(f"Sub-Agent Instructions: {len(subagent_instructions)}")
    lines.append("")
    
    # エージェント詳細（指示とツールコールを統合）
    lines.append("-" * 80)
    lines.append("AGENT DETAILS & TOOL CALLS")
    lines.append("-" * 80)
    
    agent_index = 0
    for agent in sorted(agents.values(), key=lambda x: x.get('start_time', '')):
        agent_index += 1
        agent_type = "SUB" if agent.get('is_subagent') else "MAIN"
        invoke_id = agent['invoke_id']
        
        lines.append("")
        lines.append("=" * 80)
        lines.append(f"[{agent_index}] [{agent_type}] {invoke_id}")
        lines.append("=" * 80)
        
        # 親情報
        if agent.get('parent_invoke_id'):
            lines.append(f"Parent: {agent['parent_invoke_id']}")
        
        # サブエージェントへの指示（該当する場合）
        if invoke_id in instruction_by_child:
            inst = instruction_by_child[invoke_id]
            lines.append(f"Type: {inst.get('subagent_type', 'unknown')}")
            lines.append("")
            lines.append("--- Instruction ---")
            desc = inst.get('description', '')
            # 指示は全文表示（改行を保持）
            for desc_line in desc.split('\n'):
                lines.append(f"  {desc_line}")
            lines.append("")
        
        tools = agent.get('tool_calls', [])
        
        # ツールの集計
        tool_summary = defaultdict(lambda: {'count': 0, 'total_ms': 0, 'success': 0, 'error': 0})
        for tool in tools:
            name = tool.get('tool_name', 'unknown')
            tool_summary[name]['count'] += 1
            tool_summary[name]['total_ms'] += tool.get('duration_ms') or 0
            if tool.get('status') == 'success':
                tool_summary[name]['success'] += 1
            else:
                tool_summary[name]['error'] += 1
        
        lines.append(f"--- Tool Summary ({len(tools)} calls) ---")
        if tool_summary:
            for name, stats in sorted(tool_summary.items(), key=lambda x: -x[1]['count']):
                avg_ms = stats['total_ms'] / stats['count'] if stats['count'] > 0 else 0
                lines.append(f"  {name}: {stats['count']}x (avg {avg_ms:.1f}ms, success: {stats['success']}, error: {stats['error']})")
        else:
            lines.append("  (no tool calls)")
        
        # ツール呼び出しタイムライン（全量表示）
        if tools:
            lines.append("")
            lines.append("--- Tool Calls Timeline ---")
            
            # 時間順にソート
            sorted_tools = sorted(tools, key=lambda x: x.get('start_time', ''))
            
            for i, tool in enumerate(sorted_tools, 1):
                name = tool.get('tool_name', 'unknown')
                status = 'OK' if tool.get('status') == 'success' else 'NG'
                duration = tool.get('duration_ms') or 0
                
                # 引数のプレビュー
                args = tool.get('tool_args', {})
                args_str = json.dumps(args, ensure_ascii=False)
                if len(args_str) > 100:
                    args_preview = args_str[:100] + "..."
                else:
                    args_preview = args_str
                
                lines.append(f"  {i:3}. {name} {status} ({duration:.0f}ms)")
                lines.append(f"       Args: {args_preview}")
    
    lines.append("")
    lines.append("=" * 80)
    lines.append("END OF REPORT")
    lines.append("=" * 80)
    
    return "\n".join(lines)


def extract_virtual_fs_files(agent_result: dict) -> dict:
    """
    エージェント実行結果から仮想ファイルシステムのファイルを抽出する。
    
    Args:
        agent_result: エージェント実行結果（result2など）
    
    Returns:
        dict: ファイルパスをキー、コンテンツを値とする辞書
    """
    if not agent_result:
        return {}
    
    files = agent_result.get("files", {})
    extracted = {}
    
    for path, file_data in files.items():
        # ドットから始まるファイルをスキップ
        filename = Path(path).name
        if filename.startswith('.'):
            continue
        
        # コンテンツを抽出
        if isinstance(file_data, dict):
            content = file_data.get("content", [])
            if isinstance(content, list):
                extracted[path] = "\n".join(content)
            else:
                extracted[path] = str(content)
        elif isinstance(file_data, str):
            extracted[path] = file_data
        elif isinstance(file_data, list):
            extracted[path] = "\n".join(file_data)
    
    return extracted


def analyze_agent_log(
    log_file: str = "agent_debug.jsonl",
    agent_result: Optional[dict] = None,
    status_override: Optional[str] = None,
) -> str:
    """
    エージェントログを分析し、data/runs配下に結果を保存する。
    
    Args:
        log_file: 分析するJSONLログファイル名（logディレクトリ内を検索）
        agent_result: エージェント実行結果（仮想FSを含む辞書）
        status_override: ステータスを上書きする場合に指定
    
    Returns:
        str: 生成されたテキストレポート
    
    Example:
        >>> from agent_log_analyzer import analyze_agent_log
        >>> report = analyze_agent_log("agent_debug.jsonl", agent_result=result2)
        >>> print(report)
    
    フォルダ構成:
        data/runs/【ステータス】YYYYMMDD_HHMMSS/
            agent_debug.jsonl  (バックアップ)
            agent_debug_report.txt
            out/
                (仮想FSのファイル)
    """
    # logディレクトリ内のファイルを参照
    path = LOG_DIR / log_file
    
    if not path.exists():
        raise FileNotFoundError(f"ログファイルが見つかりません: {path}")
    
    # ログ読み込み
    print(f"Loading {log_file}...")
    # NOTE: DebugLoggingMiddleware は log/ 配下に JSONL を保存するため、必ずフルパスで読む
    records = load_jsonl(str(path))
    print(f"[OK] Loaded {len(records)} records")
    
    # 解析
    print("Parsing logs...")
    parsed_data = parse_agent_logs(records)
    print(f"[OK] Found {len(parsed_data['agents'])} agents")
    print(f"[OK] Found {len(parsed_data['subagent_instructions'])} sub-agent instructions")
    
    # ステータス決定
    final_status = status_override or parsed_data.get('final_status', 'unknown')
    print(f"[OK] Final status: {final_status}")
    
    # 実行日時
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # フォルダ作成: data/runs/【ステータス】YYYYMMDD_HHMMSS/
    run_folder_name = f"【{final_status}】{timestamp}"
    run_folder = Path("data") / "runs" / run_folder_name
    run_folder.mkdir(parents=True, exist_ok=True)
    print(f"[OK] Created run folder: {run_folder}")
    
    # JSONLバックアップをコピー
    backup_path = run_folder / path.name
    shutil.copy2(path, backup_path)
    print(f"[OK] Copied log to: {backup_path}")
    
    # レポート生成
    report = generate_text_report(parsed_data, log_file, str(run_folder))
    
    # レポート保存
    report_file = run_folder / f"{path.stem}_report.txt"
    with open(report_file, 'w', encoding='utf-8') as f:
        f.write(report)
    print(f"[OK] Report saved: {report_file}")
    
    # 仮想FSのファイルを抽出・保存
    if agent_result:
        virtual_files = extract_virtual_fs_files(agent_result)
        
        if virtual_files:
            out_folder = run_folder / "out"
            out_folder.mkdir(exist_ok=True)
            
            saved_count = 0
            for vpath, content in virtual_files.items():
                # パスから /out/ などのプレフィックスを除去してファイル名を取得
                # 例: "/out/kpi_environment.md" -> "kpi_environment.md"
                vpath_obj = Path(vpath)
                # 最初のディレクトリ（例: /out）を除去
                parts = vpath_obj.parts
                if len(parts) > 1 and parts[0] in ('/', '\\', ''):
                    parts = parts[1:]  # 先頭の空白やルートを除去
                if len(parts) > 1 and parts[0] == 'out':
                    parts = parts[1:]  # /out を除去
                
                if parts:
                    # サブディレクトリ構造を維持
                    rel_path = Path(*parts)
                    dest_path = out_folder / rel_path
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    with open(dest_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                    saved_count += 1
            
            print(f"[OK] Saved {saved_count} virtual FS files to: {out_folder}")
        else:
            print("[INFO] No virtual FS files to save")
    else:
        print("[INFO] No agent_result provided, skipping virtual FS extraction")
    
    print(f"\n=== Run saved to: {run_folder} ===")
    
    return report


def get_subagent_instructions(log_file: str = "agent_debug.jsonl") -> list[dict]:
    """
    サブエージェントへの指示一覧を取得する。
    
    Args:
        log_file: ログファイル名（logディレクトリ内を検索）
    
    Returns:
        list[dict]: 各指示の情報（subagent_type, description, parent_invoke_id等）
    """
    path = LOG_DIR / log_file
    records = load_jsonl(str(path))
    parsed = parse_agent_logs(records)
    return parsed['subagent_instructions']


def get_tool_calls(log_file: str = "agent_debug.jsonl", agent_id: Optional[str] = None) -> list[dict]:
    """
    ツール呼び出しの一覧を取得する。
    
    Args:
        log_file: ログファイル名（logディレクトリ内を検索）
        agent_id: 特定のエージェントのみフィルタする場合に指定
    
    Returns:
        list[dict]: ツール呼び出し情報のリスト
    """
    path = LOG_DIR / log_file
    records = load_jsonl(str(path))
    parsed = parse_agent_logs(records)
    
    all_tools = []
    for agent in parsed['agents'].values():
        if agent_id and agent['invoke_id'] != agent_id:
            continue
        for tool in agent.get('tool_calls', []):
            all_tools.append({
                'invoke_id': agent['invoke_id'],
                'is_subagent': agent.get('is_subagent', False),
                **tool
            })
    
    return sorted(all_tools, key=lambda x: x.get('start_time', ''))


if __name__ == "__main__":
    # コマンドラインから実行する場合
    import sys
    
    log_file = sys.argv[1] if len(sys.argv) > 1 else "agent_debug.jsonl"
    report = analyze_agent_log(log_file)
    
    # Windows コンソールでの文字化け対策
    try:
        print("\n" + report)
    except UnicodeEncodeError:
        # エンコードできない文字をASCIIに置換して出力
        print("\n" + report.encode('ascii', errors='replace').decode('ascii'))
