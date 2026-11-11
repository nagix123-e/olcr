# Semantic audit: fuzzy-routing cases 1–352

- Date: 2026-10-11 (JST)
- Source runs: r35 cases 1–152, r37 cases 153–252, r39 cases 253–352
- Scope: judge whether each recorded response satisfies the prompt using the captured route, provider status/evidence, and answer text. A necessary clarification is a pass. HTTP 200, task `completed`, or provider `DATA` alone is not a pass.
- No source code or provider configuration was changed during this audit.

## Results

| Cases | PASS | FAIL | BLOCKED | UNVERIFIABLE |
|---|---:|---:|---:|---:|
| 1–152 (r35) | 0 | 0 | 0 | 152 |
| 153–252 (r37) | 8 | 57 | 35 | 0 |
| 253–352 (r39) | 9 | 72 | 19 | 0 |
| **Total** | **17** | **129** | **54** | **152** |

`BLOCKED` means the captured run did not provide enough expected-provider evidence to answer, due to an upstream/provider error, missing provider credentials, timeout, or missing dimensions in the provider response. It is not a pass, and the available logs do not always establish whether the underlying cause was OLCR argument construction or the external service. `UNVERIFIABLE` means there is not enough response evidence to make a semantic judgment.

The r35 response JSONL, database, and provider logs were removed by temporary-directory cleanup. Only recovered prompt/task metadata remains, so cases 1–152 cannot honestly be counted as passes or failures. The r37 and r39 records contain the responses and provider summaries used for this audit. The r37/r39 batch cases were submitted through the isolated backend while the GUI was open; this is a semantic result audit, not a per-case visual GUI audit. Detailed compiled provider arguments and screenshots were not retained for every case.

## Provider/transport blocks

Among the 54 blocked cases, the recorded reasons are:

| Evidence | Cases |
|---|---:|
| Provider returned empty/no usable data | 29 |
| Provider unavailable | 8 |
| Provider timeout | 5 |
| Credential setup required | 4 |
| Provider returned a bad response | 3 |
| Request timed out before a task was recorded | 1 |
| UNHCR response lacked the requested dimensions | 4 |

## Main semantic failure patterns

- Several API calls returned only provider names or generic `Result` placeholders, with no usable answer. Examples include Eurostat, EU Cellar, Brazilian and Canadian parliamentary results, BLS, and OpenFDA.
- Some completed responses used the wrong series or scope. Examples include GDP data reused for AfDB inflation/income/electricity prompts, World Bank data for a prompt explicitly requesting ADB statistics, Japan CPI for unrelated e-Stat questions, and GDP results for FRED yield questions.
- Some fallback search responses contained topical-looking material after the expected provider returned no data, but they did not supply the requested expected-source evidence.
- The r39 AfDB cases 253–256 returned the same Kenya GDP series for four different questions; the Tanzania comparison, inflation, income/employment, and electricity-access requests were therefore not answered.
- The four QuickChart results (201–204) recorded empty chart labels and datasets; a successful media response was not sufficient evidence of a usable chart.

## Passing cases

The 17 passes comprise eight useful data answers and nine appropriate clarification responses:

- Data answers: 157, 179, 189, 191, 246, 247, 334, 350.
- Clarifications: 159, 176, 275, 276, 282, 283, 294, 298, 343.

## Case-level artifact

The ignored local file `.live/olcr-e2e-semantic-audit-1-352-20261011.jsonl` contains all 352 cases with prompt, verdict, rationale, task state, route, and provider errors where available. `.live/olcr-e2e-semantic-audit-1-352-20261011-summary.json` contains the machine-readable totals. Original r37/r39 evidence files remain referenced in each case row.

This is a judgment of the captured run only. The 152 unverifiable cases need their original response/provider logs or a fresh rerun before a complete 352-case semantic pass/fail result is possible.
