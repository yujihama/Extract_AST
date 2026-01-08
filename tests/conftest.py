"""pytest設定および共通fixtures。

CLIテストで使用するテストデータのパスやセットアップを提供。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# プロジェクトルートをsys.pathに追加（compare_appのimportのため）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def project_root() -> Path:
    """プロジェクトルートパスを返す。"""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """テスト用fixtures（サンプルデータ）のディレクトリを返す。"""
    return PROJECT_ROOT / "tests" / "fixtures"


@pytest.fixture(scope="session")
def sample_doc_a(fixtures_dir: Path) -> Path:
    """サンプル文書Aのパスを返す。"""
    return fixtures_dir / "sample_doc_a.txt"


@pytest.fixture(scope="session")
def sample_doc_b(fixtures_dir: Path) -> Path:
    """サンプル文書Bのパスを返す。"""
    return fixtures_dir / "sample_doc_b.txt"


@pytest.fixture(scope="session")
def test_small_rules_v1(project_root: Path) -> Path:
    """既存の軽量テストデータv1のパスを返す。"""
    return project_root / "data" / "input" / "test_small_rules_v1.txt"


@pytest.fixture(scope="session")
def test_small_rules_v2(project_root: Path) -> Path:
    """既存の軽量テストデータv2のパスを返す。"""
    return project_root / "data" / "input" / "test_small_rules_v2.txt"


@pytest.fixture(scope="function", autouse=True)
def set_test_env():
    """テスト用の環境変数を設定する。"""
    # dotenvの自動読み込みを有効化（.envがあれば読む）
    # テスト時はDBを分離したい場合はここで設定できる
    # os.environ.setdefault("COMPARE_APP_DB_PATH", "data/test_compare_app.db")
    yield
