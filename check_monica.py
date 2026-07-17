from app import create_app, db
from app.models import EpfClient, ClientRecord, CommissionPeriod, AgentCommission

CRM_ID = "1214865071"

app = create_app()
with app.app_context():
    epf = EpfClient.query.filter_by(crm_id=CRM_ID).all()
    print(f"EpfClient rows for {CRM_ID}: {len(epf)}")
    for e in epf:
        print(f"  agent={e.agent_name!r} period={e.period_label!r} cleared_date={e.cleared_date!r} "
              f"uploaded_filename={e.uploaded_filename!r}")

    clients = ClientRecord.query.filter_by(crm_id=CRM_ID).all()
    print(f"\nClientRecord rows for {CRM_ID}: {len(clients)}")
    for c in clients:
        period = db.session.get(CommissionPeriod, c.period_id)
        print(f"  period={period.period_label if period else None!r} agent={c.agent_name!r} "
              f"is_cleared={c.is_cleared} commission_on_client={c.commission_on_client} "
              f"enrolled_debt={c.enrolled_debt}")

    period_05 = CommissionPeriod.query.filter_by(period_label="2026-05").first()
    print(f"\nCommissionPeriod 2026-05 exists: {period_05 is not None}")
    if period_05:
        print(f"  uploaded_at={period_05.uploaded_at} filename={period_05.filename}")
