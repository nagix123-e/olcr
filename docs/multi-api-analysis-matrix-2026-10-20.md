# Multi-API analysis task matrix — 2026-10-20

## 2026-10-23: active test list revised

The active matrix contains **32 cases**. Eighteen underspecified prompts were removed by user request. Original IDs are retained; no replacement prompts or renumbering were introduced. Regression, live API, and GUI E2E batches must read only `cases` from `backend/tests/fixtures/multi_api_analysis_cases.json`.

The excluded prompts and reasons are archived in `backend/tests/fixtures/multi_api_analysis_excluded_cases.json`; this archive is not an execution list. Missing mandatory subject identifiers and the resulting valid clarification responses must not count as application failures. Existing run totals below are historical, from before this revision.

Active IDs: 01, 02, 03, 04, 05, 06, 07, 08, 09, 11, 12, 13, 14, 15, 18, 19, 21, 22, 23, 24, 25, 26, 28, 29, 34, 37, 38, 39, 40, 41, 42, 48

Validation: `PYTHONPATH=backend:backend/tests .venv312/bin/python -m unittest test_multi_api_analysis_matrix test_analysis_request_scoping test_analysis_pipeline_contract` — **33 tests passed** after the exclusion and stable-ID expectation updates.

### Active prompts

| Original ID | Prompt |
|---|---|
| multi-api-analysis-01 | 日本と韓国の成長、物価、雇用の変化を比較して |
| multi-api-analysis-02 | 米国の物価と雇用の推移を比較し、世界銀行の各国GDP成長率を背景情報として確認して |
| multi-api-analysis-03 | 東南アジア各国の所得成長と貿易構成を比較して |
| multi-api-analysis-04 | アフリカ諸国の債務や成長をSDGsの進捗とあわせて |
| multi-api-analysis-05 | ラテンアメリカのインフレと成長を地域別に比較して |
| multi-api-analysis-06 | EU各国の電力価格と消費者物価、経済成長を比較して |
| multi-api-analysis-07 | 日本の輸出入の変化と為替・国際収支の動向を調べて |
| multi-api-analysis-08 | 世界の観光回復と航空便の運航規模を地域別に比較して |
| multi-api-analysis-09 | 米国の雇用・物価と連邦政府支出の変化を比較して |
| multi-api-analysis-11 | 中央銀行の金利・金融環境と物価の関係を日米欧で比較して |
| multi-api-analysis-12 | 日本の地域別人口変化と雇用状況を比較して |
| multi-api-analysis-13 | 各国の再生可能エネルギー比率と温室効果ガス排出の変化を比較して |
| multi-api-analysis-14 | 米国の電力構成と大気質の季節的な傾向を整理して |
| multi-api-analysis-15 | 大規模地震の発生状況と災害被害・支援ニーズを照合して |
| multi-api-analysis-18 | 南アジアの電力アクセスと再生可能電力の普及を比較して |
| multi-api-analysis-19 | 欧州の気温・大気汚染とエネルギー消費動向を照合して |
| multi-api-analysis-21 | 各地域の水資源ストレスと農業・持続可能性指標を比較して |
| multi-api-analysis-22 | 太陽活動と宇宙天気の変化を、観測時刻とあわせて説明して |
| multi-api-analysis-23 | 地震活動と津波・沿岸災害の記録を地域別に整理して |
| multi-api-analysis-24 | 国別の電力生産と燃料貿易の構成を比較して |
| multi-api-analysis-25 | ウクライナの紛争イベントと避難民の推移、人道支援状況を照合して |
| multi-api-analysis-26 | サヘル地域の紛争と国内避難、人道ニーズの重なりを整理して |
| multi-api-analysis-28 | 国連と米国の制裁リストの登録内容を比較し、紛争関連性を調べて |
| multi-api-analysis-29 | 難民受け入れの規模と各国の経済・人口指標を比較して |
| multi-api-analysis-34 | 中南米の暴力イベントと人道的移動・避難の状況を整理して |
| multi-api-analysis-37 | 米国の気候関連法案、規則案、連邦支出を横断して整理して |
| multi-api-analysis-38 | 日本のAI政策審議と関連法令、国会発言を比較して |
| multi-api-analysis-39 | EUの環境政策審議と法令文書、排出指標を結びつけて |
| multi-api-analysis-40 | 英国議会の医療政策議論と臨床試験の動向を整理して |
| multi-api-analysis-41 | カナダ議会の審議とカナダ・各国の経済指標を照らし、議論の背景を整理して |
| multi-api-analysis-42 | ブラジルの上下両院の審議と地域開発指標を比較して |
| multi-api-analysis-48 | AIエージェント開発記事の傾向とGitHub上の実装活動を照合して |

### Historical creation and execution record

Added 50 Japanese analysis prompts in `backend/tests/fixtures/multi_api_analysis_cases.json`.
Each asks for a cross-source comparison and names two or three complementary API IDs.
All 50 API combinations are distinct and every API ID is present in the implemented
runtime registry.

The matrix covers economic indicators and trade, energy and natural hazards,
conflict and humanitarian needs, public policy, health research, security, and
biodiversity. `backend/tests/test_multi_api_analysis_matrix.py` feeds every task
through the autonomous analysis runner and verifies the selected APIs are each
compiled, called, and passed together with provenance-bearing results to the
analysis composition step.

Run from the repository root:

```sh
PYTHONPATH=backend:backend/tests .venv312/bin/python -m unittest test_multi_api_analysis_matrix.MultiApiAnalysisMatrixTests
```

This regression matrix uses deterministic provider fixtures. A separate live
adapter run was then performed once per case/API pair. Of 150 planned pairs,
24 were skipped because credentials were not configured and 27 stopped at
argument compilation; 99 real provider adapters were called. Those calls
returned 50 usable data envelopes, 30 provider errors, and 19 empty-data
envelopes. The dominant errors were unsupported provider capabilities (16),
missing/invalid compiled arguments (27), provider unavailability (6), and
timeouts (5). These results show the adapter surface and data-quality gaps;
the live runner invokes adapters directly with the task prompt, so it does not
exercise Laya/Brain source-specific planning or GUI E2E routing.

Sanitized live results are kept outside the repository at
`/private/tmp/olcr-multi-api-live-e2e-20261020-run-01/`.

The analysis pipeline now treats an empty provider envelope as a failed source
and invokes the existing Web/Wikipedia fallback when some planned sources fail
even if other APIs returned evidence. Requests that fail only because required
user-specified arguments are missing still remain clarification cases.
