import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from olcr_api import app, external_tools


class ApiAnalysisSummaryE2ERepairTests(unittest.TestCase):
    def test_unspecified_trade_trend_uses_a_bounded_completed_year_window(self):
        args = external_tools.compile_provider_arguments(
            "trade.un_comtrade", "日本の輸出入の変化と為替の動向を調べて"
        )
        last_year = datetime.now(timezone.utc).year - 1
        self.assertEqual(
            list(range(last_year - 4, last_year + 1)),
            [int(period) for period in args["periods"].split(",")],
        )
        self.assertTrue(args["include_both_flows"])

    def test_trade_preview_requests_one_year_per_call_and_combines_flows(self):
        text = "日本の輸出入の変化と為替・国際収支の動向を調べて"
        args = external_tools.compile_provider_arguments("trade.un_comtrade", text)
        years = args["periods"].split(",")
        flows = ["X", "M"]
        responses = []
        for year in years:
            for flow in flows:
                responses.append({"data": [{
                    "period": year, "reporterCode": 392, "reporterDesc": "Japan",
                    "partnerDesc": "World", "flowCode": flow,
                    "flowDesc": "Export" if flow == "X" else "Import",
                    "cmdCode": "TOTAL", "primaryValue": 700_000_000_000,
                }]})

        with patch.object(external_tools, "_request", side_effect=responses) as request:
            result = external_tools.execute("trade.un_comtrade", args)

        self.assertEqual(len(years) * 2, request.call_count)
        self.assertEqual(years, [call.args[2]["period"] for call in request.call_args_list[::2]])
        self.assertTrue(all("," not in call.args[2]["period"] for call in request.call_args_list))
        self.assertEqual({"X", "M"}, {row["flow_code"] for row in result["data"]["items"]})
        self.assertTrue(result["data"]["request_complete"])

    def test_unspecified_japan_exchange_trend_uses_jpy_usd_pair(self):
        args = external_tools.compile_provider_arguments(
            "currency.frankfurter", "日本の為替動向を調べて"
        )
        self.assertEqual("JPY", args["base"])
        self.assertEqual("USD", args["quote"])

    def test_explicit_all_currencies_keeps_frankfurter_unrestricted_quote(self):
        args = external_tools.compile_provider_arguments(
            "currency.frankfurter", "日本の全通貨為替レートを調べて"
        )
        self.assertEqual("JPY", args["base"])
        self.assertIsNone(args["quote"])

    def test_analysis_call_telemetry_records_resolved_provider_model(self):
        result = {
            "model": "gemini-3.6-flash", "model_role": "normal_chat",
            "model_provenance": {
                "model_id": "api:google:gemini-3.6-flash",
                "deployment_revision": "UNKNOWN", "runtime": "google",
            },
        }
        with patch.object(app.brain_models, "selected_model_for_role", return_value="api:google:gemini-3.6-flash"):
            call = app._analysis_model_call_metadata(result, "brain_analysis_planner", "success")
        self.assertEqual("api:google:gemini-3.6-flash", call["model"])
        self.assertEqual("api:google:gemini-3.6-flash", call["requested_model"])
        self.assertEqual("google", call["model_runtime"])

    def test_compact_japanese_export_import_request_fetches_both_flows(self):
        text = "2025年の日本の輸出入を確認して"
        args = external_tools.compile_provider_arguments("trade.un_comtrade", text)
        self.assertTrue(args["include_both_flows"])
        export = {"data": [{"period": "2025", "reporterCode": 392, "reporterDesc": "Japan",
                            "partnerDesc": "World", "flowCode": "X", "flowDesc": "Export",
                            "cmdCode": "TOTAL", "primaryValue": 700_000_000_000}]}
        imports = {"data": [{"period": "2025", "reporterCode": 392, "reporterDesc": "Japan",
                             "partnerDesc": "World", "flowCode": "M", "flowDesc": "Import",
                             "cmdCode": "TOTAL", "primaryValue": 750_000_000_000}]}
        with patch.object(external_tools, "_request", side_effect=[export, imports]) as request:
            result = external_tools.execute("trade.un_comtrade", args)
        self.assertEqual(2, request.call_count)
        self.assertEqual({"X", "M"}, {row["flow_code"] for row in result["data"]["items"]})
        self.assertTrue(result["data"]["request_complete"])

    def test_successful_trade_flow_is_kept_when_the_second_flow_fails(self):
        args = external_tools.compile_provider_arguments("trade.un_comtrade", "2025年の日本の輸出入を確認して")
        export = {"data": [{"period": "2025", "reporterCode": 392, "reporterDesc": "Japan",
                            "partnerDesc": "World", "flowCode": "X", "flowDesc": "Export",
                            "cmdCode": "TOTAL", "primaryValue": 700_000_000_000}]}
        with patch.object(external_tools, "_request", side_effect=[export, external_tools.ExternalToolError("PROVIDER_TIMEOUT")]):
            result = external_tools.execute("trade.un_comtrade", args)
        self.assertEqual(1, len(result["data"]["items"]))
        self.assertFalse(result["data"]["request_complete"])
        self.assertEqual(["M"], result["data"]["missing_flow_codes"])

    def test_small_nonzero_currency_rates_are_not_rounded_to_zero(self):
        evidence = [{
            "tool_id": "currency.frankfurter", "provider": "Frankfurter",
            "sources": [{"source_id": "FX"}],
            "data": {"items": [
                {"date": "2026-01-01", "indicator": "JPY/AUD", "value": 0.0090733, "unit": "AUD/JPY"},
                {"date": "2026-10-01", "indicator": "JPY/AUD", "value": 0.0100012, "unit": "AUD/JPY"},
                {"date": "2026-01-01", "indicator": "JPY/BTC", "value": 0.000000076148, "unit": "BTC/JPY"},
                {"date": "2026-01-01", "indicator": "JPY/CHF", "value": 0.018000000000000002, "unit": "CHF/JPY"},
                {"date": "2026-10-01", "indicator": "JPY/CHF", "value": 0.02, "unit": "CHF/JPY"},
            ]},
        }]
        answer = app._deterministic_tabular_analysis(evidence, "日本の為替動向を分析して")
        self.assertIn("0.0090733AUD/JPY", answer)
        self.assertIn("0.0100012AUD/JPY", answer)
        self.assertIn("7.6148e-8BTC/JPY", answer)
        self.assertIn("+0.002CHF/JPY", answer)
        self.assertNotIn("+0.0020000000", answer)
        self.assertNotIn("+0AUD/JPY", answer)

    def test_trade_value_and_multiple_providers_survive_summary_budget(self):
        currency_rows = []
        for index in range(20):
            quote = f"Q{index:02d}"
            currency_rows.extend([
                {"date": "2026-01-01", "indicator": f"JPY/{quote}", "value": 0.01 + index / 100, "unit": f"{quote}/JPY"},
                {"date": "2026-10-01", "indicator": f"JPY/{quote}", "value": 0.02 + index / 100, "unit": f"{quote}/JPY"},
            ])
        evidence = [
            {"tool_id": "currency.frankfurter", "provider": "Frankfurter",
             "sources": [{"source_id": "FX"}], "data": {"items": currency_rows}},
            {"tool_id": "trade.un_comtrade", "provider": "UN Comtrade",
             "sources": [{"source_id": "TRADE"}], "data": {"items": [
                 {"year": "2022", "reporter": "Japan", "flow": "Export", "commodity": "TOTAL", "trade_value_usd": 700000000000},
                 {"year": "2025", "reporter": "Japan", "flow": "Export", "commodity": "TOTAL", "trade_value_usd": 738195990796},
             ]}},
        ]
        answer = app._deterministic_tabular_analysis(evidence, "日本の輸出と為替の変化を分析して")
        self.assertIn("UN Comtradeの全品目の輸出額", answer)
        self.assertIn("738195990796", answer)
        self.assertIn("FrankfurterのJPY/Q00", answer)
        self.assertIn("詳細", answer)


if __name__ == "__main__":
    unittest.main()
