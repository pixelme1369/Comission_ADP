"""Shared CRM-import persistence logic used by both drive_sync.py (automated
daily Drive sync) and the admin manual-CSV-upload fallback, so the two entry
points can never drift on how a parsed CRM export gets saved to the DB.
Mirrors app/routes.py's upload_crm handler and _new_client_record helper."""

from agent_portal import db
from agent_portal.models import (
    AgentCommission, ClientRecord, CommissionPeriod,
    CordobaChargedBackClient, CordobaPaidClient,
)
from commission_core.calculator import agent_identity_key


def bulk_delete_period(period):
    """Deletes one CommissionPeriod and everything under it via a handful of
    bulk `DELETE ... WHERE` statements, instead of `db.session.delete(period)`
    relying on the model's `cascade="all, delete-orphan"` relationships.

    That ORM cascade path measurably does NOT scale: to cascade-delete it has
    to first SELECT every AgentCommission for the period, then SELECT every
    ClientRecord for EACH of those agents individually (an N+1 — one query
    per agent), and then — the expensive part — delete every row one at a
    time. SQLAlchemy batches those per-row deletes into a single
    `executemany()` call, but psycopg2's `executemany()` is not a real
    server-side batch operation: it silently loops and sends one individual
    statement per row. Verified directly against PostgreSQL's own
    server-side log: deleting a period with 5 agents and 100 ClientRecord
    rows produced exactly 100 separate `DELETE FROM client_record WHERE
    id = <single row>` lines, not one. A real CRM upload can easily be an
    order of magnitude bigger than that per period — and the "Delete &
    Reset" button on the dashboard does this once per month in the file.

    Filtering directly on ClientRecord.period_id and AgentCommission.period_id
    (both indexed, non-nullable FKs — see models.py) collapses that down to
    three bulk statements total, regardless of how many rows are involved.
    synchronize_session=False is safe here because nothing touches the
    session's in-memory copies of these rows afterward — the caller commits
    and moves on. Does NOT commit — same contract as `db.session.delete()`,
    caller commits."""
    ClientRecord.query.filter_by(period_id=period.id).delete(synchronize_session=False)
    AgentCommission.query.filter_by(period_id=period.id).delete(synchronize_session=False)
    db.session.delete(period)


def _new_client_record(period_id, agent_commission_id, cr, **overrides):
    fields = dict(
        period_id=period_id,
        agent_commission_id=agent_commission_id,
        crm_id=cr.get("crm_id"),
        agent_name=cr["agent_name"],
        client_name=cr.get("client_name"),
        email=cr.get("email"),
        phone=cr.get("phone"),
        stage=cr.get("stage"),
        status=cr.get("status"),
        submitted_date=cr.get("submitted_date"),
        enrolled_date=cr.get("enrolled_date"),
        first_payment_date=cr.get("first_payment_date"),
        first_payment_cleared_date=cr.get("first_payment_cleared_date"),
        second_payment_cleared_date=cr.get("second_payment_cleared_date"),
        dropped_date=cr.get("dropped_date"),
        pay_freq=cr.get("pay_freq"),
        payments_made=cr.get("payments_made", 0),
        nsf_count=cr.get("nsf_count", 0),
        enrolled_debt=cr.get("enrolled_debt", 0.0),
        # Only ever set by commission_history_parser.py's "cleared" rows (Rate
        # column) — a CRM-parsed client dict has no "paid_rate" key at all, so this
        # is just None for every CRM-originated ClientRecord. See known_rate_by_crm_id.
        paid_rate=cr.get("paid_rate"),
        credit_score=cr.get("credit_score"),
        is_low_credit=cr.get("is_low_credit", False),
        is_cleared=cr.get("is_cleared", False),
        is_pending=cr.get("is_pending", False),
        is_cancelled=cr.get("is_cancelled", False),
        commission_on_client=cr.get("commission_on_client", 0.0),
    )
    fields.update(overrides)
    return ClientRecord(**fields)


def already_known_crm_id_sets():
    """crm_ids this DB already knows about, for the parser's late-activation /
    clawback-guard / low-credit-guard / already-paid-via-history logic.
    already_charged_back comes from BOTH the Cordoba chargeback ledger (see
    cordoba_ingest.py) AND every ClientRecord already clawed back via a PRIOR
    CRM upload — owner policy, confirmed August 2026: clawbacks now land in
    the client's own Dropped Date month (not "latest period in file"), which
    is stable/unchanging across every future upload, so without the
    CRM-sourced half of this set, a full-history re-upload would re-detect
    and re-apply the SAME clawback every single time (the target period
    never advances forward the way "latest period in file" used to). See
    commission_core/crm_parser.py's module docstring for the full mechanism.
    already_history_paid comes from Commission History imports specifically
    (source="history_import" periods only) — owner policy, confirmed August
    2026: "This file has already been paid. Don't calculate it again. Only
    watch it going forward to see if it drops and needs a clawback." See
    commission_core/crm_parser.py's module docstring for the full mechanism."""
    already_cleared = {
        r[0] for r in db.session.query(ClientRecord.crm_id)
        .filter(ClientRecord.is_cleared.is_(True)) if r[0]
    }
    already_charged_back = {
        r[0] for r in db.session.query(CordobaChargedBackClient.crm_id) if r[0]
    } | {
        r[0] for r in db.session.query(ClientRecord.crm_id)
        .filter(ClientRecord.clawback_applied.is_(True)) if r[0]
    }
    already_low_credit = {
        r[0] for r in db.session.query(ClientRecord.crm_id)
        .filter(ClientRecord.is_low_credit.is_(True)) if r[0]
    }
    already_history_paid = {
        r[0] for r in db.session.query(ClientRecord.crm_id)
        .join(CommissionPeriod, ClientRecord.period_id == CommissionPeriod.id)
        .filter(ClientRecord.is_cleared.is_(True), CommissionPeriod.source == "history_import")
        if r[0]
    }
    return already_cleared, already_charged_back, already_low_credit, already_history_paid


def known_enrolled_debt_by_crm_id():
    """crm_id -> the Enrolled Debt that was ACTUALLY used to calculate that
    client's original commission (Commission History import or an earlier CRM
    period) — owner policy, confirmed: a clawback must be based on THIS, never
    on whatever a later CRM re-export happens to show for the same crm_id
    today. Confirmed real case: a client paid via Commission History with
    Enrolled Debt $30,688 showed Enrolled Debt $2,664.62 in a later CRM
    export's row for the same crm_id — Cordoba's own systems evidently revise
    this figure over time. See known_enrolled_debt_by_crm_id's own docstring
    on parse_crm_and_calculate for the full mechanism; this is the query that
    builds it, same original-clear record (is_cleared=True) already consulted
    for already_known_crm_id_sets() above."""
    return {
        r[0]: r[1] for r in db.session.query(ClientRecord.crm_id, ClientRecord.enrolled_debt)
        .filter(ClientRecord.is_cleared.is_(True)) if r[0]
    }


def known_rate_by_crm_id():
    """crm_id -> the exact rate a Commission-History-paid client's original
    commission was actually paid at (owner-added "Rate" column on Commission
    History import files, e.g. "1.40%") — only ever set on ClientRecord rows
    that came from a Commission History import (see ClientRecord.paid_rate;
    a CRM-computed "cleared" client has no such column and stays NULL, so
    never appears here). Used to claw back enrolled_debt * paid_rate verbatim
    instead of recalculating a rate through the tier table. See
    known_rate_by_crm_id's own docstring on parse_crm_and_calculate for the
    full mechanism."""
    return {
        r[0]: r[1] for r in db.session.query(ClientRecord.crm_id, ClientRecord.paid_rate)
        .filter(ClientRecord.is_cleared.is_(True), ClientRecord.paid_rate.isnot(None)) if r[0]
    }


def known_period_totals():
    """(agent_identity_key(agent_name), period_label) -> {"units_cleared",
    "total_cleared_debt", "gross_commission", "cancellation_rate"} — the DB's
    actual saved totals for every already-saved AgentCommission row, SUMMED
    across every source sharing that period_label (agent_portal allows a
    "crm" period and a "history_import" period to coexist for the same month
    — see CommissionPeriod's docstring in models.py) AND across every raw
    agent_name spelling sharing the same case/whitespace-insensitive identity
    (the CRM export and the Commission History file are parsed independently,
    so nothing stops one from saving "Amir Moayeri" and a later upload of the
    other saving "amir moayeri" for the literal same real agent — a
    confirmed real case; keyed by agent_identity_key so both still combine
    into one entry here regardless).

    Bug fix (confirmed against a live case, see commission_core/crm_parser.py's
    module docstring item 4): parse_crm_and_calculate()'s Step 3 needs to know
    what an agent's tier/commission actually was in their original cleared
    month to compute a later clawback against it. It gets that by recomputing
    Steps 1-2 from THIS SAME file's own rows — accurate for a month that's
    entirely CRM-computed, but NOT accurate for a month mostly or entirely
    backfilled via Commission History, since already_history_paid_crm_ids
    deliberately excludes those already-paid clients from that recomputation
    (to avoid double-crediting them). Passing this dict in as
    known_period_totals lets Step 3 use the DB's real, authoritative numbers
    instead whenever they exist, rather than trusting a same-file
    recomputation that can be missing most or all of that month's real
    activity.

    cancellation_rate can't be meaningfully summed across sources (it's
    already a percentage, not a raw count) — this takes it from whichever
    source row contributes the most units, so a tiny leftover holding row
    (e.g. one Credit Score <= 500 client with its own 0% rate) never
    overrides the real period's rate. Ties keep whichever row is seen last
    from the query (order is not significant here — the tier-drop penalty
    this feeds only matters at the >20% boundary either way)."""
    rows = (
        db.session.query(
            AgentCommission.agent_name, CommissionPeriod.period_label,
            AgentCommission.units_cleared, AgentCommission.total_cleared_debt,
            AgentCommission.gross_commission, AgentCommission.cancellation_rate,
        )
        .join(CommissionPeriod, AgentCommission.period_id == CommissionPeriod.id)
        .all()
    )
    totals = {}
    dominant_units = {}
    for agent_name, period_label, units, debt, gross, cxl_rate in rows:
        key = (agent_identity_key(agent_name), period_label)
        entry = totals.setdefault(key, {
            "units_cleared": 0, "total_cleared_debt": 0.0,
            "gross_commission": 0.0, "cancellation_rate": 0.0,
        })
        entry["units_cleared"] += units or 0
        entry["total_cleared_debt"] += debt or 0.0
        entry["gross_commission"] += gross or 0.0
        if (units or 0) >= dominant_units.get(key, -1):
            entry["cancellation_rate"] = cxl_rate or 0.0
            dominant_units[key] = units or 0
    return totals


def _get_or_create_crm_agent_row(period, agent_name):
    """Find (or create a zero-unit) AgentCommission row to carry a clawback
    that has no cleared units of its own in this period, tagged
    source="crm" — the CRM-upload equivalent of cordoba_ingest.py's
    _get_or_create_agent_period_row (source="cordoba" there), used now that
    CRM-driven clawbacks can land in an already-existing period just as
    often as Cordoba-driven ones can."""
    agent_row = AgentCommission.query.filter_by(period_id=period.id, agent_name=agent_name).first()
    if not agent_row:
        agent_row = AgentCommission(
            period_id=period.id, agent_name=agent_name,
            units_cleared=0, total_cleared_debt=0.0, cancellation_rate=0.0, hourly_draw=0.0,
            raw_tier=0, adjusted_tier=0, tier_rate=0.0, gross_commission=0.0,
            clawback_amount=0.0, net_commission=0.0, payout=0.0, payout_type="none",
            quality_bonus_eligible=False, cancellation_penalty_applied=False, nsf_flagged=False,
            pending_units=0, pending_debt=0.0, source="crm", notes="",
        )
        db.session.add(agent_row)
        db.session.flush()
        period.total_agents = (period.total_agents or 0) + 1
    return agent_row


def save_period_results(period_results, filename, source_label="drive"):
    """Persist parse_crm_and_calculate()'s output. Does NOT commit — caller
    commits (so drive_sync.py can add its SyncedFile ledger row in the same
    transaction). Returns {"periods_created": [...], "warnings": [...]}."""
    periods_created = []
    warnings = []
    seen_errors = set()

    # A client already confirmed paid via a prior Cordoba First Pays/EPF
    # upload should come in pre-flagged even though this file predates it.
    already_cordoba_paid_ids = {r[0] for r in db.session.query(CordobaPaidClient.crm_id)}

    for parsed in period_results:
        for err in parsed.get("errors", []):
            if err not in seen_errors:
                warnings.append(err)
                seen_errors.add(err)

        if not parsed["results"] or not parsed["period_label"]:
            continue

        period_label = parsed["period_label"]
        # Scoped to source="crm" — a "history_import" period for this same label
        # (backfilled reference data, a separate dataset by design — see
        # CommissionPeriod's docstring) must never block a real calculated
        # period from being created for the same month.
        existing = CommissionPeriod.query.filter_by(period_label=period_label, source="crm").first()

        if not existing:
            period = CommissionPeriod(
                period_label=period_label, filename=filename, total_agents=len(parsed["results"]),
                source="crm",
            )
            db.session.add(period)
            db.session.flush()

            agent_obj_map = {}
            for r in parsed["results"]:
                all_period_clients = r.pop("_all_period_clients", [])
                clawback_clients = r.pop("_clawback_clients", [])
                r.pop("_cleared_clients", None)
                r.pop("_period_label", None)
                r["source"] = source_label

                agent_obj = AgentCommission(period_id=period.id, **r)
                db.session.add(agent_obj)
                db.session.flush()
                agent_obj_map[r["agent_name"]] = {
                    "obj": agent_obj,
                    "all_period_clients": all_period_clients,
                    "clawback_clients": clawback_clients,
                }

            for agent_name, data in agent_obj_map.items():
                agent_obj = data["obj"]
                for cr in data["all_period_clients"]:
                    db.session.add(_new_client_record(
                        period.id, agent_obj.id, cr,
                        is_late_activation=cr.get("is_late_activation", False),
                        original_cleared_period=cr.get("original_cleared_period"),
                        cordoba_paid=cr.get("crm_id") in already_cordoba_paid_ids,
                    ))
                for cr in data["clawback_clients"]:
                    db.session.add(_new_client_record(
                        period.id, agent_obj.id, cr,
                        is_cleared=False, is_pending=False, is_cancelled=True,
                        commission_on_client=0.0, clawback_applied=True,
                        clawback_period_id=period.id, clawback_amount=cr.get("clawback_amount", 0.0),
                    ))

            periods_created.append(period_label)
            continue

        # Period already exists. Genuine new cleared/safe-cancel activity for that
        # month is NOT re-imported here (unchanged protection against double-counting
        # an already-recorded month — delete it first to re-import). But any NEW
        # clawback found for this month IS still applied: now that clawbacks target
        # the client's own Dropped Date month (owner policy, confirmed August 2026),
        # that month is very often one that already exists on file — that's no longer
        # a reason to silently lose the clawback. Applied via find-or-create, mirroring
        # how the separate Cordoba-chargeback flow has always attached a deduction to
        # an existing period (cordoba_ingest.py's _get_or_create_agent_period_row).
        period_had_new_units = any(r.get("units_cleared", 0) > 0 for r in parsed["results"])
        new_clawback_total = 0.0
        new_clawback_count = 0

        for r in parsed["results"]:
            clawback_clients = r.get("_clawback_clients", [])
            if not clawback_clients:
                continue

            agent_name = r["agent_name"]
            cb_ids = {c["crm_id"] for c in clawback_clients if c.get("crm_id")}
            already_recorded = {
                x[0] for x in db.session.query(ClientRecord.crm_id).filter(
                    ClientRecord.crm_id.in_(cb_ids),
                    ClientRecord.clawback_applied.is_(True),
                )
            } if cb_ids else set()
            new_cb = [c for c in clawback_clients
                      if not c.get("crm_id") or c["crm_id"] not in already_recorded]
            if not new_cb:
                continue

            agent_row = _get_or_create_crm_agent_row(existing, agent_name)
            total_cb = round(sum(c.get("clawback_amount", 0.0) for c in new_cb), 2)
            agent_row.clawback_amount = round((agent_row.clawback_amount or 0.0) + total_cb, 2)
            agent_row.net_commission = max(0.0, round(agent_row.gross_commission - agent_row.clawback_amount, 2))
            agent_row.notes = (agent_row.notes or "") + \
                f" | Clawback -${total_cb:,.2f} from {len(new_cb)} previously-paid cancelled client(s)"

            for cr in new_cb:
                db.session.add(_new_client_record(
                    existing.id, agent_row.id, cr,
                    is_cleared=False, is_pending=False, is_cancelled=True,
                    commission_on_client=0.0, clawback_applied=True,
                    clawback_period_id=existing.id, clawback_amount=cr.get("clawback_amount", 0.0),
                ))
            new_clawback_total += total_cb
            new_clawback_count += len(new_cb)

        if period_had_new_units:
            warnings.append(
                f"Period {period_label} already exists — new cleared/safe-cancel activity for that "
                "month was NOT re-imported. Delete it first if you need to re-import that month's "
                "calculated commissions."
            )
        if new_clawback_count:
            warnings.append(
                f"Period {period_label} already existed — applied {new_clawback_count} new "
                f"clawback(s) totaling ${new_clawback_total:,.2f} to it."
            )

    return {"periods_created": periods_created, "warnings": warnings}


def group_periods_by_filename(periods):
    """Groups CommissionPeriod rows by their source filename (a multi-month CRM
    export or history-ledger upload can create several periods from one file),
    for the admin dashboard's per-upload-type "recent uploads" lists — newest
    upload first."""
    groups = {}
    for p in periods:
        groups.setdefault(p.filename or "(unknown)", []).append(p)
    return sorted(
        ({"filename": name, "periods": sorted(rows, key=lambda r: r.period_label)}
         for name, rows in groups.items()),
        key=lambda g: max(r.uploaded_at for r in g["periods"]), reverse=True,
    )


def delete_periods_by_filename(filename, source_values):
    """Deletes every CommissionPeriod with this filename whose agents were
    created by one of the given upload types (source_values, e.g. ("drive",
    "manual") for a CRM upload or ("history_import",) for a history backfill)
    — lets the admin dashboard delete/reset an entire multi-month upload in
    one action instead of one period at a time. Returns the list of
    period_labels actually deleted.

    Uses bulk_delete_period (see there for why) instead of
    db.session.delete(period)'s ORM cascade — this matters even more here
    than deleting a single period, since a multi-month CRM export runs this
    once per month it contains. The source check below also queries just the
    one column needed instead of loading each period's full `.agents`
    collection (which would re-introduce the same N+1 this function exists
    to avoid, just one level up)."""
    periods = CommissionPeriod.query.filter_by(filename=filename).all()
    deleted_labels = []
    for period in periods:
        first_source = (
            db.session.query(AgentCommission.source)
            .filter_by(period_id=period.id).first()
        )
        if first_source and first_source[0] in source_values:
            deleted_labels.append(period.period_label)
            bulk_delete_period(period)
    db.session.commit()
    return deleted_labels
