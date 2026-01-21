from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Scenario:
    id: str
    request: str
    docs: list[Path]


def run_cmd(argv: list[str], cwd: Path) -> str:
    """Run a command and return stdout; raise with context on failure."""
    p = subprocess.run(
        argv,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        raise RuntimeError(
            "Command failed\n"
            f"  argv: {argv}\n"
            f"  cwd: {cwd}\n"
            f"  exit: {p.returncode}\n"
            f"  stdout:\n{p.stdout}\n"
            f"  stderr:\n{p.stderr}\n"
        )
    return p.stdout.strip()


def ensure_exists(paths: list[Path]) -> None:
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError("Missing input files:\n" + "\n".join(str(p) for p in missing))


def _load_dotenv_if_available(repo_root: Path) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(repo_root / ".env")
    except Exception:
        # dotenv が無い/読めない場合でも、OS環境変数があれば動く
        return


def _has_api_key() -> bool:
    import os

    return bool(os.getenv("OPENAI_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["dummy", "real"], default="dummy")
    p.add_argument(
        "--phase",
        choices=["all", "create", "execute", "export"],
        default="all",
        help="Which phase to run. 'create' writes create.json, 'execute' runs pipeline, 'export' exports artifacts.",
    )
    p.add_argument(
        "--ids",
        nargs="*",
        default=[],
        help="Run only specific scenario IDs (e.g. 01_compliance 02_gap). If omitted, runs all.",
    )
    p.add_argument(
        "--out",
        default="",
        help="Output directory. Default: data/usecase/cli_scenarios (dummy) or data/usecase/cli_scenarios_real (real).",
    )
    # real向けのコスト抑制デフォルト（必要ならCLI側でoverride）
    p.add_argument("--llm-complex-model", default="")
    p.add_argument("--summarize-ast", action="store_true", help="Enable summarize_ast (default off for real).")
    p.add_argument("--no-summarize-ast", action="store_true", help="Disable summarize_ast.")
    p.add_argument(
        "--recursion-limit",
        type=int,
        default=0,
        help="LangGraph recursion_limit override (0 means default).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    repo_root = Path(__file__).resolve().parent.parent
    if args.out:
        out_root = (repo_root / args.out).resolve()
    else:
        out_root = (
            repo_root
            / "data"
            / "usecase"
            / ("cli_scenarios_real" if args.mode == "real" else "cli_scenarios")
        )
    out_root.mkdir(parents=True, exist_ok=True)

    data_in = repo_root / "data" / "input"

    scenarios: list[Scenario] = [
        Scenario(
            id="01_compliance",
            request="次の文書群を基準に準拠評価を行い、未整備/要改善項目と根拠（該当箇所の引用）を列挙してください。",
            docs=[
                data_in / "standard_security_baseline.txt",
                data_in / "policy_it_security.txt",
                data_in / "procedure_access_request.txt",
                data_in / "procedure_vendor_onboarding.txt",
                data_in / "procedure_key_management.txt",
            ],
        ),
        Scenario(
            id="02_gap",
            request="基準に対する対応状況を 未対応/部分対応/対応済み で整理し、主要なギャップと根拠を示してください。",
            docs=[
                data_in / "standard_security_baseline.txt",
                data_in / "policy_it_security.txt",
                data_in / "procedure_access_request.txt",
                data_in / "procedure_vendor_onboarding.txt",
                data_in / "procedure_key_management.txt",
            ],
        ),
        Scenario(
            id="03_change_impact",
            request="v1→v2、v2→v3の差分と改訂影響を分類し、運用/契約/監査への影響と対応案を提示してください。",
            docs=[
                data_in / "contract_service_v1.txt",
                data_in / "contract_service_v2.txt",
                data_in / "contract_service_v3.txt",
            ],
        ),
        Scenario(
            id="04_traceability",
            request="要求→設計→テストのトレーサビリティ（対応表）を作成し、欠落/部分対応や用語ゆれを指摘してください。",
            docs=[
                data_in / "requirements_product_x.txt",
                data_in / "design_product_x.txt",
                data_in / "test_spec_product_x.txt",
            ],
        ),
        Scenario(
            id="05_drafting",
            request="template_disclosure_draft.md のテンプレ構造に沿ってドラフトを作成し、入力情報を各セクションへ反映してください。",
            docs=[
                data_in / "company_profile.txt",
                data_in / "product_overview_x.txt",
                data_in / "risk_register_q4.txt",
                data_in / "template_disclosure_draft.md",
            ],
        ),
        Scenario(
            id="06_extraction",
            request="義務/禁止/期限/例外/罰則を抽出し、表形式またはJSONで構造化してください。",
            docs=[data_in / "privacy_addendum_sample.txt"],
        ),
        Scenario(
            id="07_quality",
            request="矛盾、参照切れ、例外期限不備、承認不備、用語ゆれを検出し、指摘事項と根拠を提示してください。",
            docs=[
                data_in / "single_policy_with_issues.txt",
                data_in / "glossary_terms.txt",
            ],
        ),
        Scenario(
            id="08_summarization",
            request="経営向け/現場向け/監査向けの3種類で、要点（影響・原因・再発防止・意思決定）を要約してください。",
            docs=[data_in / "incident_postmortem_2026_01.txt"],
        ),
        Scenario(
            id="09_qa",
            request="questions_product_x.txt の各質問に対して、回答と根拠（該当箇所の引用）を添えて回答してください。",
            docs=[
                data_in / "faq_product_x.txt",
                data_in / "policy_it_security.txt",
                data_in / "contract_service_v2.txt",
                data_in / "questions_product_x.txt",
            ],
        ),
        Scenario(
            id="10_translation",
            request="この文書を英訳し、重要な事実（原因/影響/対策）が保持されていることを確認してください。",
            docs=[data_in / "jp_security_notice.txt"],
        ),
        # ========================================
        # 拡張シナリオ: 新規ドメイン
        # ========================================
        # 医療ドメイン: 準拠評価
        Scenario(
            id="11_healthcare_compliance",
            request="医療情報システム安全管理ガイドラインに対する病院IT規程の準拠状況を評価し、"
            "未整備/要改善項目と根拠（MED-01〜MED-10の各要求との差分）を列挙してください。",
            docs=[
                data_in / "healthcare_regulation_baseline.txt",
                data_in / "hospital_it_policy.txt",
            ],
        ),
        # 金融AMLドメイン: 準拠評価
        Scenario(
            id="12_aml_compliance",
            request="マネー・ローンダリング対策ガイドラインに対する銀行AML規程の準拠状況を評価し、"
            "未整備/要改善項目と根拠（AML-01〜AML-10の各要求との差分）を列挙してください。",
            docs=[
                data_in / "aml_regulation_baseline.txt",
                data_in / "bank_aml_policy.txt",
            ],
        ),
        # 人事労務ドメイン: ギャップ分析
        Scenario(
            id="13_labor_gap",
            request="労働法の法的要件に対する就業規則の対応状況を 未対応/部分対応/対応済み で整理し、"
            "主要なギャップ（育児休業、ハラスメント対応、割増賃金等）と根拠を示してください。",
            docs=[
                data_in / "labor_law_baseline.txt",
                data_in / "company_work_rules_v1.txt",
            ],
        ),
        # 製造品質ドメイン: 準拠評価
        Scenario(
            id="14_qms_compliance",
            request="ISO 9001:2015要求事項に対する品質マニュアルの準拠状況を評価し、"
            "適合/部分適合/不適合の判定と改善機会を示してください。",
            docs=[
                data_in / "iso9001_requirements.txt",
                data_in / "manufacturing_qms_manual.txt",
            ],
        ),
        # 医療ドメイン: 差分分析（治験プロトコル）
        Scenario(
            id="15_clinical_trial_change",
            request="治験プロトコルv1→v2の変更点（用量漸増デザイン、対象患者基準、サイクル期間、"
            "症例数、バイオマーカー評価等）を分類し、IRB再審査・被験者再同意への影響を評価してください。",
            docs=[
                data_in / "clinical_trial_protocol_v1.txt",
                data_in / "clinical_trial_protocol_v2.txt",
            ],
        ),
        # 人事労務ドメイン: 差分分析（就業規則）
        Scenario(
            id="16_work_rules_change",
            request="就業規則v1→v2の変更点（試用期間、フレックス、テレワーク、割増賃金、再雇用年齢、副業等）を分類し、"
            "従業員周知・労基署届出・雇用契約書改定への影響を評価してください。",
            docs=[
                data_in / "company_work_rules_v1.txt",
                data_in / "company_work_rules_v2.txt",
            ],
        ),
        # 金融ドメイン: トレーサビリティ（与信審査システム）
        Scenario(
            id="17_credit_traceability",
            request="与信審査システムの要件定義→基本設計→テスト仕様のトレーサビリティ（対応表）を作成し、"
            "欠落/部分対応（特にREQ-N05公平性テストの網羅性等）を指摘してください。",
            docs=[
                data_in / "credit_scoring_requirements.txt",
                data_in / "credit_scoring_design.txt",
                data_in / "credit_scoring_test_spec.txt",
            ],
        ),
        # 製造品質ドメイン: 品質点検（仕様書 vs 検査手順）
        Scenario(
            id="18_spec_inspection_quality",
            request="製品仕様書と検査手順書の整合性を点検し、"
            "検査項目の過不足、判定基準の不一致、参照切れを検出してください。",
            docs=[
                data_in / "product_spec_part_a.txt",
                data_in / "inspection_procedure.txt",
            ],
        ),
        # 医療ドメイン: 抽出（同意説明文書）
        Scenario(
            id="19_consent_extraction",
            request="同意説明文書から、研究目的、予想されるリスク、予想される利益、費用負担、"
            "補償条件、連絡先を抽出し、表形式またはJSONで構造化してください。",
            docs=[data_in / "patient_consent_form.txt"],
        ),
        # ========================================
        # 拡張シナリオ: 新規ユースケース
        # ========================================
        # 提案評価・ベンダー比較
        Scenario(
            id="20_proposal_evaluation",
            request="RFP要件に対する各ベンダー提案（A社、B社）の適合度を評価し、"
            "技術提案・プロジェクト管理・運用保守・費用の観点で比較表を作成し、"
            "評価基準に基づくスコアリングと推奨ベンダー（根拠付き）を提示してください。",
            docs=[
                data_in / "rfp_cloud_system.txt",
                data_in / "proposal_vendor_a.txt",
                data_in / "proposal_vendor_b.txt",
                data_in / "proposal_evaluation_criteria.txt",
            ],
        ),
        # M&A デューデリジェンス統合
        Scenario(
            id="21_ma_dd_integration",
            request="対象会社概要と法務DD・財務DD報告書を統合分析し、"
            "リスク項目の優先度付け、価格調整・表明保証条項への反映事項、"
            "クロージング前/後の対応事項リストを作成してください。",
            docs=[
                data_in / "ma_target_company_profile.txt",
                data_in / "ma_legal_dd_checklist.txt",
                data_in / "ma_legal_dd_findings.txt",
                data_in / "ma_financial_dd_findings.txt",
            ],
        ),
    ]

    wanted = set(str(x).strip() for x in (args.ids or []) if str(x).strip())
    if wanted:
        scenarios = [s for s in scenarios if s.id in wanted]
        missing_ids = sorted(wanted - {s.id for s in scenarios})
        if missing_ids:
            raise ValueError(f"Unknown scenario ids: {missing_ids}")

    # realモードはAPIキー必須（.env も読む）
    if args.mode == "real":
        _load_dotenv_if_available(repo_root)
        if not _has_api_key():
            raise RuntimeError(
                "realモードの実行にはAPIキーが必要です。"
                " .env に OPENAI_API_KEY, AZURE_OPENAI_API_KEY, または GOOGLE_API_KEY を設定してください。"
            )

    for s in scenarios:
        ensure_exists(s.docs)
        scenario_dir = out_root / s.id
        scenario_dir.mkdir(parents=True, exist_ok=True)

        run_id: str | None = None

        # ---- phase: create ----
        if args.phase in {"all", "create"}:
            params: dict[str, Any] = {"mode": args.mode}
            if args.mode == "real":
                # LLM_PROVIDERに応じてデフォルトモデルを選択
                default_model = "gpt-5-mini"
                if os.getenv("LLM_PROVIDER") == "gemini":
                    default_model = os.getenv("GEMINI_MODEL") or "gemini-2.0-flash"
                llm_complex_model = str(args.llm_complex_model or "").strip() or default_model
                params["llm_complex_model"] = llm_complex_model

                # real: summarize_ast はユーザ指定を優先（未指定ならデフォルトOFF）
                if args.summarize_ast:
                    params["summarize_ast"] = True
                elif args.no_summarize_ast:
                    params["summarize_ast"] = False
                else:
                    params["summarize_ast"] = False
                if int(args.recursion_limit or 0) > 0:
                    params["recursion_limit"] = int(args.recursion_limit)

            create_argv = [
                sys.executable,
                "-m",
                "document_process_app.cli",
                "create",
                "--mode",
                args.mode,
                "--request",
                s.request,
                "--params",
                json.dumps(params, ensure_ascii=False),
            ]
            for d in s.docs:
                create_argv += ["--doc", str(d)]

            create_out = run_cmd(create_argv, cwd=repo_root)
            (scenario_dir / "create.json").write_text(create_out + "\n", encoding="utf-8")
            create_obj = json.loads(create_out)
            run_id = str(create_obj["run_id"])
            (scenario_dir / "run_id.txt").write_text(run_id + "\n", encoding="utf-8")
            print(f"=== {s.id} run_id={run_id} ===", flush=True)
        else:
            # create済みのrun_idを読む
            run_id_path = scenario_dir / "run_id.txt"
            if run_id_path.exists():
                run_id = run_id_path.read_text(encoding="utf-8", errors="replace").strip()
            else:
                create_path = scenario_dir / "create.json"
                if not create_path.exists():
                    raise FileNotFoundError(f"Missing create.json for scenario: {s.id}")
                run_id = str(json.loads(create_path.read_text(encoding="utf-8"))["run_id"])

        assert run_id is not None and run_id.strip()

        # ---- phase: execute ----
        if args.phase in {"all", "execute"}:
            execute_out = run_cmd([sys.executable, "-m", "document_process_app.cli", "execute", run_id], cwd=repo_root)
            (scenario_dir / "execute.json").write_text(execute_out + "\n", encoding="utf-8")

        # ---- phase: export ----
        if args.phase in {"all", "export"}:
            artifacts_out = run_cmd([sys.executable, "-m", "document_process_app.cli", "artifacts", run_id], cwd=repo_root)
            (scenario_dir / "artifacts.json").write_text(artifacts_out + "\n", encoding="utf-8")

            export_filled_out = run_cmd(
                [
                    sys.executable,
                    "-m",
                    "document_process_app.cli",
                    "export",
                    run_id,
                    "--kind",
                    "template_filled",
                    "--out",
                    str(scenario_dir / "template_filled.md"),
                ],
                cwd=repo_root,
            )
            (scenario_dir / "export_template_filled.json").write_text(export_filled_out + "\n", encoding="utf-8")

            export_events_out = run_cmd(
                [
                    sys.executable,
                    "-m",
                    "document_process_app.cli",
                    "export",
                    run_id,
                    "--kind",
                    "events_log_jsonl",
                    "--out",
                    str(scenario_dir / "events.jsonl"),
                ],
                cwd=repo_root,
            )
            (scenario_dir / "export_events_log_jsonl.json").write_text(export_events_out + "\n", encoding="utf-8")

    print(f"\nDONE: outputs written to {out_root}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

