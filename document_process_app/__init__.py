"""FastAPI/HTMX/CLI で共通利用するアプリ層パッケージ。

既存の `src/` はPoCのコアロジック（AST/比較/ツール等）を提供し、
この `document_process_app/` は「Run管理・イベント・永続化・UI/CLI統合」を担当する想定。
"""

