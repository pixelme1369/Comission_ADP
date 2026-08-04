"""Pulls the newest CRM export snapshot from the Cordoba_ADP Google Drive
folder and runs it through the same parser/persistence path as a manual CSV
upload (see ingest.py). Each day's export is a full-history snapshot (same
shape the CRM system has always produced), named like "998427-<random>" and
stored as a Google Sheet — so it's downloaded via Drive's CSV export, not a
raw file download. Triggered by an admin "Sync Now" click or the Vercel Cron
schedule in vercel.json."""

import io
import json

from flask import current_app

from agent_portal import db
from commission_core.crm_parser import parse_crm_and_calculate
from agent_portal.ingest import already_known_crm_id_sets, save_period_results
from agent_portal.models import SyncedFile

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CSV_EXPORT_MIME = "text/csv"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _drive_service():
    # Imported lazily: google-api-python-client/google-auth are only needed
    # when an actual sync runs, not just to boot the app — keeps them from
    # being a hard dependency for routes/tests that never touch Drive.
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not configured. See README.md for "
            "one-time Google Cloud service-account setup."
        )
    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_newest_file(service, folder_id):
    resp = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        orderBy="modifiedTime desc",
        pageSize=1,
        fields="files(id, name, mimeType, modifiedTime)",
    ).execute()
    files = resp.get("files", [])
    return files[0] if files else None


def _download_csv_bytes(service, file_meta):
    from googleapiclient.http import MediaIoBaseDownload

    file_id = file_meta["id"]
    if file_meta["mimeType"] == GOOGLE_SHEET_MIME:
        media_request = service.files().export_media(fileId=file_id, mimeType=CSV_EXPORT_MIME)
    else:
        media_request = service.files().get_media(fileId=file_id)

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, media_request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def sync_from_drive():
    """Returns {"synced": bool, "message": str, "periods_created": [...], "warnings": [...]}."""
    folder_id = current_app.config["DRIVE_FOLDER_ID"]
    service = _drive_service()

    file_meta = _find_newest_file(service, folder_id)
    if not file_meta:
        return {
            "synced": False,
            "message": f"No files found in Drive folder {folder_id}.",
            "periods_created": [], "warnings": [],
        }

    already = SyncedFile.query.filter_by(drive_file_id=file_meta["id"]).first()
    if already and already.drive_modified_time == file_meta["modifiedTime"]:
        return {
            "synced": False,
            "message": f"Already up to date — newest file '{file_meta['name']}' was already synced.",
            "periods_created": [], "warnings": [],
        }

    file_bytes = _download_csv_bytes(service, file_meta)

    already_cleared, already_charged_back, already_low_credit, already_history_paid = already_known_crm_id_sets()
    period_results = parse_crm_and_calculate(
        file_bytes, file_meta["name"], already_cleared, already_charged_back, already_low_credit,
        already_history_paid,
        # agent_portal-specific policy flags — see commission_core/crm_parser.py's
        # module docstring for the owner-confirmed reasoning behind all three.
        persist_same_month_cancel=True,
        require_prior_payment_evidence=False,
    )

    outcome = save_period_results(period_results, file_meta["name"], source_label="drive")

    db.session.add(SyncedFile(
        drive_file_id=file_meta["id"],
        drive_file_name=file_meta["name"],
        drive_modified_time=file_meta["modifiedTime"],
        periods_created=len(outcome["periods_created"]),
    ))
    db.session.commit()

    periods = outcome["periods_created"]
    message = (
        f"Synced '{file_meta['name']}': {len(periods)} period(s) created ({', '.join(periods)})."
        if periods else
        f"Synced '{file_meta['name']}': no new periods (all already existed)."
    )
    return {"synced": True, "message": message, "periods_created": periods, "warnings": outcome["warnings"]}
