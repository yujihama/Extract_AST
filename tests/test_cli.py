"""CLIコマンドのテストスイート。

compare_app.cli モジュールのコマンドをテストする。
dummyモードでの軽量テストを中心に、パイプラインの基本動作を検証。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


class TestCliCreate:
    """'create' コマンドのテスト。"""

    def test_create_run_dummy_mode(self, sample_doc_a: Path, sample_doc_b: Path):
        """dummyモードでRunを作成できることを確認する。"""
        result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create",
                "--doc-a", str(sample_doc_a),
                "--doc-b", str(sample_doc_b),
                "--mode", "dummy",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        
        # JSON出力をパース
        output = json.loads(result.stdout)
        assert "run_id" in output
        assert output["status"] == "queued"
        assert "created_at" in output

    def test_create_run_with_existing_test_data(self, test_small_rules_v1: Path, test_small_rules_v2: Path):
        """既存の軽量テストデータでRunを作成できることを確認する。"""
        result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create",
                "--doc-a", str(test_small_rules_v1),
                "--doc-b", str(test_small_rules_v2),
                "--mode", "dummy",
            ],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        output = json.loads(result.stdout)
        assert "run_id" in output


class TestCliExecute:
    """'execute' コマンド（同期実行）のテスト。"""

    def test_execute_dummy_mode_succeeds(self, sample_doc_a: Path, sample_doc_b: Path):
        """dummyモードでRun作成→同期実行が成功することを確認する。"""
        cwd = str(Path(__file__).resolve().parent.parent)
        
        # Run作成
        create_result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create",
                "--doc-a", str(sample_doc_a),
                "--doc-b", str(sample_doc_b),
                "--mode", "dummy",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        assert create_result.returncode == 0
        run_id = json.loads(create_result.stdout)["run_id"]
        
        # 同期実行
        execute_result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "execute", run_id,
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        
        assert execute_result.returncode == 0, f"stdout: {execute_result.stdout}\nstderr: {execute_result.stderr}"
        output = json.loads(execute_result.stdout)
        assert output["ok"] is True
        assert output["run_id"] == run_id

    def test_execute_creates_artifacts(self, sample_doc_a: Path, sample_doc_b: Path):
        """実行後に成果物（template_filled.md等）が生成されることを確認する。"""
        cwd = str(Path(__file__).resolve().parent.parent)
        
        # Run作成
        create_result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create",
                "--doc-a", str(sample_doc_a),
                "--doc-b", str(sample_doc_b),
                "--mode", "dummy",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        run_id = json.loads(create_result.stdout)["run_id"]
        
        # 同期実行
        subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "execute", run_id],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        
        # 成果物一覧を確認
        artifacts_result = subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "artifacts", run_id],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        
        assert artifacts_result.returncode == 0
        artifacts = json.loads(artifacts_result.stdout)
        
        # 成果物にtemplate_draftとtemplate_filledが含まれるか確認
        kinds = [a.get("kind") for a in artifacts]
        assert "template_draft" in kinds or any("template_draft" in str(a.get("path", "")) for a in artifacts)
        assert "template_filled" in kinds or any("template_filled" in str(a.get("path", "")) for a in artifacts)


class TestCliList:
    """'list' コマンドのテスト。"""

    def test_list_runs(self, sample_doc_a: Path, sample_doc_b: Path):
        """Run一覧が取得できることを確認する。"""
        cwd = str(Path(__file__).resolve().parent.parent)
        
        # まず1件作成
        subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create",
                "--doc-a", str(sample_doc_a),
                "--doc-b", str(sample_doc_b),
                "--mode", "dummy",
            ],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        
        # 一覧取得
        result = subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "list"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        
        assert result.returncode == 0
        runs = json.loads(result.stdout)
        assert isinstance(runs, list)
        assert len(runs) >= 1


class TestCliArtifacts:
    """'artifacts' コマンドのテスト。"""

    def test_artifacts_after_execute(self, sample_doc_a: Path, sample_doc_b: Path):
        """実行後の成果物一覧が取得できることを確認する。"""
        cwd = str(Path(__file__).resolve().parent.parent)
        
        # 作成→実行
        create_result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create", "--doc-a", str(sample_doc_a), "--doc-b", str(sample_doc_b), "--mode", "dummy",
            ],
            capture_output=True, text=True, cwd=cwd,
        )
        run_id = json.loads(create_result.stdout)["run_id"]
        
        subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "execute", run_id],
            capture_output=True, text=True, cwd=cwd,
        )
        
        # 成果物取得
        result = subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "artifacts", run_id],
            capture_output=True, text=True, cwd=cwd,
        )
        
        assert result.returncode == 0
        artifacts = json.loads(result.stdout)
        assert isinstance(artifacts, list)
        assert len(artifacts) > 0  # 何らかの成果物があるはず


class TestCliExport:
    """'export' コマンドのテスト。"""

    def test_export_template_filled(self, sample_doc_a: Path, sample_doc_b: Path, tmp_path: Path):
        """template_filledをエクスポートできることを確認する。"""
        cwd = str(Path(__file__).resolve().parent.parent)
        
        # 作成→実行
        create_result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create", "--doc-a", str(sample_doc_a), "--doc-b", str(sample_doc_b), "--mode", "dummy",
            ],
            capture_output=True, text=True, cwd=cwd,
        )
        run_id = json.loads(create_result.stdout)["run_id"]
        
        subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "execute", run_id],
            capture_output=True, text=True, cwd=cwd,
        )
        
        # エクスポート
        out_path = tmp_path / "exported_template.md"
        result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "export", run_id, "--kind", "template_filled", "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=cwd,
        )
        
        assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
        output = json.loads(result.stdout)
        assert output["ok"] is True
        assert out_path.exists()
        
        # 内容を確認
        content = out_path.read_text(encoding="utf-8")
        assert "生成テンプレ" in content or "テンプレ" in content


class TestCliEndToEnd:
    """エンドツーエンドのテスト。"""

    def test_full_dummy_workflow(self, test_small_rules_v1: Path, test_small_rules_v2: Path, tmp_path: Path):
        """既存の軽量テストデータでcreate→execute→artifacts→exportの全フローが動作することを確認する。"""
        cwd = str(Path(__file__).resolve().parent.parent)
        
        # 1. Run作成
        create_result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create",
                "--doc-a", str(test_small_rules_v1),
                "--doc-b", str(test_small_rules_v2),
                "--mode", "dummy",
            ],
            capture_output=True, text=True, cwd=cwd,
        )
        assert create_result.returncode == 0
        run_id = json.loads(create_result.stdout)["run_id"]
        print(f"\n[Test] Created run: {run_id}")
        
        # 2. 同期実行
        execute_result = subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "execute", run_id],
            capture_output=True, text=True, cwd=cwd,
        )
        assert execute_result.returncode == 0
        print(f"[Test] Execute result: {execute_result.stdout}")
        
        # 3. 成果物一覧
        artifacts_result = subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "artifacts", run_id],
            capture_output=True, text=True, cwd=cwd,
        )
        assert artifacts_result.returncode == 0
        artifacts = json.loads(artifacts_result.stdout)
        print(f"[Test] Artifacts count: {len(artifacts)}")
        
        # 4. template_filled エクスポート
        out_path = tmp_path / "result.md"
        export_result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "export", run_id, "--kind", "template_filled", "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=cwd,
        )
        assert export_result.returncode == 0
        assert out_path.exists()
        
        content = out_path.read_text(encoding="utf-8")
        print(f"[Test] Exported template preview: {content[:200]}...")
        
        # 5. Run一覧に含まれるか確認
        list_result = subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "list"],
            capture_output=True, text=True, cwd=cwd,
        )
        runs = json.loads(list_result.stdout)
        run_ids = [r["run_id"] for r in runs]
        assert run_id in run_ids


class TestCliErrors:
    """エラーケースのテスト。"""

    def test_execute_nonexistent_run(self):
        """存在しないrun_idでexecuteするとエラーになることを確認する。"""
        cwd = str(Path(__file__).resolve().parent.parent)
        
        result = subprocess.run(
            [sys.executable, "-m", "compare_app.cli", "execute", "nonexistent_run_id"],
            capture_output=True, text=True, cwd=cwd,
        )
        
        # エラーになるはず
        assert result.returncode != 0 or '"ok": false' in result.stdout

    def test_export_nonexistent_kind(self, sample_doc_a: Path, sample_doc_b: Path, tmp_path: Path):
        """存在しないkindでexportするとエラーになることを確認する。"""
        cwd = str(Path(__file__).resolve().parent.parent)
        
        # 作成（実行はしない）
        create_result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "create", "--doc-a", str(sample_doc_a), "--doc-b", str(sample_doc_b), "--mode", "dummy",
            ],
            capture_output=True, text=True, cwd=cwd,
        )
        run_id = json.loads(create_result.stdout)["run_id"]
        
        # 実行しないとtemplate_filledは存在しない
        out_path = tmp_path / "should_not_exist.md"
        result = subprocess.run(
            [
                sys.executable, "-m", "compare_app.cli",
                "export", run_id, "--kind", "template_filled", "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=cwd,
        )
        
        # ファイルが存在しないのでエラーになるはず
        assert result.returncode != 0 or '"ok": false' in result.stdout
