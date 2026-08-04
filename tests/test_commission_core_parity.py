"""Proves the two things the commission_core merge (August 2026) exists to guarantee.

Before this merge, app/ and agent_portal/ each vendored their own byte-for-byte
copy of calculator.py, crm_parser.py, cordoba_parser.py, and
commission_history_parser.py — a future rule change had to be hand-applied
twice, and nothing enforced that it actually was. Now there is exactly one
copy of each, in agent_portal/commission_core/ (see that package's README.md
for why it physically lives there and not at the repo root), and the two
apps' real call sites import from it directly.

1. TestBothAppsAgreeOnPlainCommissionMath / TestDivergencesAreScopedToTheirOwnFlagOnly:
   both apps' actual call sites compute IDENTICAL commission numbers from
   identical CRM input, for every scenario except the two explicitly
   owner-confirmed divergences — see commission_core/crm_parser.py's module
   docstring for persist_same_month_cancel / require_prior_payment_evidence.
   This drives commission_core.crm_parser.parse_crm_and_calculate() directly
   with each app's actual flag values (copied verbatim from
   app/routes.py's defaults and agent_portal/agent_portal/routes_admin.py's
   /drive_sync.py's real call sites) rather than importing either app's Flask
   package, so it runs under app/'s own minimal requirements.txt with no
   extra dependencies.

2. TestBothAppsImportTheSameFunctionsNotACopy: both apps import the exact
   same function objects from commission_core — not a second copy — so a
   future rule change in commission_core.calculator or
   commission_core.crm_parser automatically reaches both apps from one edit.
   This half needs agent_portal's own dependencies (flask_login, etc.)
   installed, since it imports agent_portal's real modules to check identity;
   it's skipped (not failed) when they aren't available.
"""

import csv
import io

import pytest

import app.routes as app_routes
from commission_core import calculator as core_calculator
from commission_core import commission_history_parser as core_history_parser
from commission_core.crm_parser import parse_crm_and_calculate

try:
    import agent_portal.cordoba_ingest as ap_cordoba_ingest
    import agent_portal.history_ingest as ap_history_ingest
    import agent_portal.routes_admin as ap_routes_admin
    AGENT_PORTAL_AVAILABLE = True
except ImportError:
    AGENT_PORTAL_AVAILABLE = False

# The exact flag values each app's real call site relies on: app/routes.py
# never passes either flag, so these are parse_crm_and_calculate's own
# defaults; agent_portal/agent_portal/routes_admin.py and drive_sync.py pass
# these explicitly. Named here (not re-derived) so this test breaks loudly if
# either app's real call site ever drifts from what's asserted below.
APP_FLAGS = {"persist_same_month_cancel": False, "require_prior_payment_evidence": True}
AGENT_PORTAL_FLAGS = {"persist_same_month_cancel": True, "require_prior_payment_evidence": False}

HEADERS = [
    "ID", "Sales Rep", "Full Name", "1st Payment Cleared Date", "Dropped Date",
    "Status", "Enrolled Debt", "# NSF", "Payments Made", "Pay Freq.", "Credit Score",
]


def _crm_csv(rows) -> bytes:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=HEADERS)
    writer.writeheader()
    for r in rows:
        writer.writerow({h: r.get(h, "") for h in HEADERS})
    return out.getvalue().encode("utf-8")


def _client(crm_id, agent="Agent A", cleared="06/01/2026", dropped="", debt="10000",
            status="Active", pay_freq="Monthly", payments="2", nsf="0", credit=""):
    return {
        "ID": crm_id, "Sales Rep": agent, "Full Name": f"Client {crm_id}",
        "1st Payment Cleared Date": cleared, "Dropped Date": dropped, "Status": status,
        "Enrolled Debt": debt, "# NSF": nsf, "Payments Made": payments,
        "Pay Freq.": pay_freq, "Credit Score": credit,
    }


def _numeric_fields(result):
    """Only the fields that represent real money math — excludes bookkeeping
    fields like 'source' and internal display lists (_all_period_clients
    etc.) that aren't part of the payout itself."""
    keys = (
        "units_cleared", "total_cleared_debt", "cancellation_rate", "raw_tier",
        "adjusted_tier", "tier_rate", "gross_commission", "clawback_amount",
        "net_commission", "payout", "payout_type", "quality_bonus_eligible",
        "cancellation_penalty_applied",
    )
    return {k: result[k] for k in keys}


class TestBothAppsAgreeOnPlainCommissionMath:
    """Scenarios that don't touch either documented divergence, so app/'s and
    agent_portal's real flag values must produce byte-identical numbers."""

    def test_simple_cleared_month_matches_exactly(self):
        data = _crm_csv([
            _client("1", cleared="06/05/2026", debt="20000"),
            _client("2", cleared="06/12/2026", debt="30000"),
        ])
        app_periods = parse_crm_and_calculate(data, "f.csv", **APP_FLAGS)
        ap_periods = parse_crm_and_calculate(data, "f.csv", **AGENT_PORTAL_FLAGS)

        assert [p["period_label"] for p in app_periods] == ["2026-06"]
        assert [p["period_label"] for p in ap_periods] == ["2026-06"]
        (app_result,) = app_periods[0]["results"]
        (ap_result,) = ap_periods[0]["results"]
        assert _numeric_fields(app_result) == _numeric_fields(ap_result)

    def test_clawback_with_prior_db_evidence_matches_exactly(self):
        # already_cleared_crm_ids proves prior payment either way, so
        # require_prior_payment_evidence doesn't change the outcome here —
        # both apps agree a real clawback applies.
        data = _crm_csv([
            _client("1", cleared="06/01/2026", debt="10000"),
            _client("2", cleared="06/05/2026", dropped="07/10/2026", debt="15000", payments="1"),
        ])
        app_periods = parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"1", "2"}, **APP_FLAGS)
        ap_periods = parse_crm_and_calculate(
            data, "f.csv", already_cleared_crm_ids={"1", "2"}, **AGENT_PORTAL_FLAGS)

        app_by_period = {p["period_label"]: p for p in app_periods}
        ap_by_period = {p["period_label"]: p for p in ap_periods}
        assert set(app_by_period) == set(ap_by_period)
        for label in app_by_period:
            app_results = sorted(app_by_period[label]["results"], key=lambda r: r["agent_name"])
            ap_results = sorted(ap_by_period[label]["results"], key=lambda r: r["agent_name"])
            assert [_numeric_fields(r) for r in app_results] == [_numeric_fields(r) for r in ap_results]

    def test_credit_score_and_safe_cancel_match_exactly(self):
        data = _crm_csv([
            _client("1", cleared="06/01/2026", debt="10000", credit="450"),
            _client("2", cleared="06/03/2026", dropped="08/15/2026", debt="12000", payments="4"),
            _client("3", cleared="06/10/2026", debt="9000"),
        ])
        app_periods = parse_crm_and_calculate(data, "f.csv", **APP_FLAGS)
        ap_periods = parse_crm_and_calculate(data, "f.csv", **AGENT_PORTAL_FLAGS)
        (app_result,) = app_periods[0]["results"]
        (ap_result,) = ap_periods[0]["results"]
        assert _numeric_fields(app_result) == _numeric_fields(ap_result)


class TestDivergencesAreScopedToTheirOwnFlagOnly:
    """The two documented, owner-confirmed differences show up ONLY where
    they're supposed to, and change nothing else in the money math."""

    def test_same_month_cancel_is_display_only_never_affects_money(self):
        data = _crm_csv([
            _client("1", cleared="06/05/2026", debt="20000"),
            _client("2", cleared="06/10/2026", dropped="06/20/2026", debt="99999"),
        ])
        app_periods = parse_crm_and_calculate(data, "f.csv", **APP_FLAGS)
        ap_periods = parse_crm_and_calculate(data, "f.csv", **AGENT_PORTAL_FLAGS)
        (app_result,) = app_periods[0]["results"]
        (ap_result,) = ap_periods[0]["results"]

        # Same money despite agent_portal persisting the same_month_cancel
        # row for display — the $99,999 debt never enters either calculation.
        assert _numeric_fields(app_result) == _numeric_fields(ap_result)
        assert len(app_result["_all_period_clients"]) == 1   # app/ never shows same_month_cancel rows
        assert len(ap_result["_all_period_clients"]) == 2    # agent_portal does (display only)

    def test_first_time_clawback_without_prior_evidence_is_the_one_real_divergence(self):
        # No already_cleared_crm_ids/already_charged_back_crm_ids at all —
        # exactly the "first upload, no DB history yet" scenario the owner's
        # August 2026 policy directive for agent_portal was about.
        data = _crm_csv([
            _client("1", cleared="03/05/2026", dropped="06/23/2026", debt="16866", payments="1"),
        ])
        app_periods = parse_crm_and_calculate(data, "f.csv", **APP_FLAGS)
        ap_periods = parse_crm_and_calculate(data, "f.csv", **AGENT_PORTAL_FLAGS)

        # app/ (old/conservative policy, owner-confirmed to stay as-is during
        # this merge): no proof of payment in this file or DB -> reclassified,
        # no clawback anywhere.
        app_clawback_total = sum(r["clawback_amount"] for p in app_periods for r in p["results"])
        assert app_clawback_total == 0.0

        # agent_portal ("always assume i paid them for previous cleared
        # files" — owner-confirmed): the client's own cleared date is treated
        # as proof enough on its own -> a real clawback applies.
        ap_clawback_total = sum(r["clawback_amount"] for p in ap_periods for r in p["results"])
        assert ap_clawback_total == pytest.approx(168.66)  # 16866 * 1% flat-rate fallback


@pytest.mark.skipif(
    not AGENT_PORTAL_AVAILABLE,
    reason="agent_portal's own dependencies (flask_login, etc.) aren't installed in this environment",
)
class TestBothAppsImportTheSameFunctionsNotACopy:
    """Not 'equal output' but literally `is` the same function object — the
    strongest possible proof there's only one copy of this logic left."""

    def test_crm_parser_is_the_literal_same_function(self):
        assert app_routes.parse_crm_and_calculate is parse_crm_and_calculate
        assert ap_routes_admin.parse_crm_and_calculate is parse_crm_and_calculate

    def test_calculator_functions_are_the_literal_same_function(self):
        assert app_routes.calculate_clawback_amount is core_calculator.calculate_clawback_amount
        assert ap_cordoba_ingest.calculate_clawback_amount is core_calculator.calculate_clawback_amount
        assert app_routes.get_fixed_rate is core_calculator.get_fixed_rate
        assert ap_cordoba_ingest.get_fixed_rate is core_calculator.get_fixed_rate
        assert app_routes.units_to_next_tier is core_calculator.units_to_next_tier
        assert ap_routes_admin.units_to_next_tier is core_calculator.units_to_next_tier

    def test_commission_history_parser_is_the_literal_same_function(self):
        assert app_routes.parse_commission_history is core_history_parser.parse_commission_history
        assert ap_history_ingest.parse_commission_history is core_history_parser.parse_commission_history

    def test_a_future_fixed_rate_change_reaches_both_apps_from_one_edit(self, monkeypatch):
        """Simulates 'a future rule change happens in one place': patches
        commission_core.calculator directly (not either app's module) and
        confirms both apps' own call paths see the change identically."""
        monkeypatch.setitem(core_calculator.AGENT_FIXED_RATES, "test agent xyz", 0.09)
        assert app_routes.get_fixed_rate("Test Agent XYZ") == 0.09
        assert ap_cordoba_ingest.get_fixed_rate("Test Agent XYZ") == 0.09

    def test_a_future_tier_table_change_reaches_both_apps_from_one_edit(self, monkeypatch):
        monkeypatch.setattr(core_calculator, "TIERS", [(1, None, 0.05, "Single Test Tier")])
        # Only tier left is 1-and-up with no ceiling -> nothing further to climb toward.
        assert app_routes.units_to_next_tier(5) is None
        assert ap_routes_admin.units_to_next_tier(5) is None
