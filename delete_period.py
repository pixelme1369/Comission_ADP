from app import create_app, db
from app.models import CommissionPeriod

app = create_app()
with app.app_context():
    period = CommissionPeriod.query.filter_by(period_label="2026-05").first()
    if period:
        db.session.delete(period)
        db.session.commit()
        print("Deleted period 2026-05")
    else:
        print("No period 2026-05 found")
