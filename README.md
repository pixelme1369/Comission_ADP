# ADP Commission Calculator

A web application for calculating and tracking agent commissions at American Debt Protection, based on the April 2026 Commission Plan.

---

## How to Run

```bash
cd /Users/saman/Documents/GitHub/Comission_ADP
source .venv/bin/activate
python run.py
```

Open your browser and go to **http://127.0.0.1:5000**

To stop the server: press **CTRL+C** in the terminal.

---

## How to Use the App

### Step 1 — Upload Your CRM Export

On the home page, use the **"Upload CRM Export"** form (left side). Select the CSV file you export from the backend CRM system and click **Import CRM Data**.

The system will automatically:
- Group all clients by their Sales Rep (agent)
- Identify which clients have cleared their first payment
- Calculate each agent's commission tier and dollar amount
- Flag any clawbacks, NSF issues, or pending units

> **One period per upload.** If your CRM file spans multiple months, the system will split them automatically.

---

## How to Read the Results Page

After uploading, you will see a results page for the commission period. Here is what everything means:

### Summary Cards (top of page)

| Card | What it means |
|---|---|
| **Agents** | Number of agents with activity this period |
| **Gross Commission** | Total commission earned before any clawbacks |
| **Total Clawbacks** | Amount deducted this period due to prior-month cancellations |
| **Total Commission Owed** | What you actually owe agents this period (Gross − Clawbacks) |
| **Quality Bonus Eligible** | Agents whose cancellation rate is below 10% — eligible for $500 bonus (requires manual approval) |
| **Tier Penalties** | Agents whose tier was dropped due to cancellation rate above 20% |
| **NSF Flagged** | Agents who have at least one client with 3 or more NSF events |
| **Pending Units** | Agents with clients held in "Pending Affiliate Cancellation" status — not paid yet |

### Payment Date

Commissions shown for a given month are **paid on the 25th of the following month.**

- Example: clients clearing in **May** → agent is paid on **June 25th**
- Example: clients clearing in **June** → agent is paid on **July 25th**

### Results Table — Column by Column

| Column | What it means |
|---|---|
| **Agent** | Click the agent's name to see their full client breakdown |
| **Units** | Number of clients whose first payment cleared this month |
| **Tier** | Commission tier earned. If a penalty was applied it shows as "2→1" (dropped from Tier 2 to Tier 1) |
| **Rate** | Commission percentage for this tier |
| **Cleared Debt** | Total enrolled debt of all cleared clients for this agent |
| **Gross Commission** | Cleared Debt × Rate |
| **Clawback** | Amount deducted because a prior-month client cancelled with fewer than 3 payments |
| **Net Commission** | What you actually owe this agent (Gross − Clawback) |
| **Cancel Rate** | Percentage of the agent's clients who cancelled this period |
| **Quality Bonus** | "Rate OK" means cancellation rate is below 10% — still requires your manual review before paying the $500 bonus |
| **NSF** | "Flag" means at least one client has 3+ NSF events — review before paying |
| **Pending** | Number of clients held due to "Pending Affiliate Cancellation" status |

### Row Colors

| Color | Meaning |
|---|---|
| 🟢 Green | Agent qualifies for quality bonus (cancellation rate < 10%) |
| 🟡 Amber | Agent receives draw only — commission was below their hourly draw |
| 🔴 Red | Tier penalty applied (cancel rate > 20%) or clawback deducted |
| 🟠 Orange | NSF flag on one or more clients |
| 🟣 Purple | Agent has pending units waiting on Affiliate Cancellation review |

---

## Agent Detail Page

Click any agent's name to see a full breakdown of every individual client. This is what you show an agent if they ask "how are you calculating my commission?"

The page is divided into sections:

### Cleared Clients
Clients whose first payment cleared this month and are being paid commission on. Shows:
- Client name, enrolled debt, commission earned on that client
- Payment dates, payments made, NSF count, contact info

### Pending — Affiliate Cancellation Review
Clients whose first payment cleared but whose status is "Pending Affiliate Cancellation." **Commission is not paid** on these until the status resolves to active.

### Clawbacks Applied This Period
Clients who were paid commission in a **prior month** but cancelled this month with fewer than 3 payments. The commission originally paid on them has been deducted from this period's payout.

### Cancelled Same Month — Not Paid
Clients who cleared and cancelled in the **same month**. No commission was ever paid on these, so there is no clawback — they are simply excluded.

---

## Commission Rules Summary

### Tier Table

| Units Cleared | Tier | Rate | Club |
|---|---|---|---|
| 1 – 20 | Tier 1 | 1.00% | — |
| 21 – 31 | Tier 2 | 1.25% | — |
| 32 – 39 | Tier 3 | 1.50% | — |
| 40 – 45 | Tier 4 | 1.75% | President's Club |
| 46 – 60 | Tier 5 | 2.00% | Chairman's Club |
| 61+ | Tier 6 | 2.25% | Legacy Club |

- The tier is based on **total units clearing their first payment** in the calendar month
- Once a tier is reached, **all** cleared debt for that month is paid at that rate

### Cancellation Rate Rules
- Cancellation rate **above 20%** → tier is dropped by one level for that period
- Cancellation rate **below 10%** → agent is flagged as eligible for $500 Quality Performance Bonus (requires manual approval)

### Clawback Rules
- If a client cancels in a **later month** than when they cleared → commission is clawed back
- Clawback only applies if the client has **fewer than 3 payments made**
- If removing the cancelled client drops the agent's tier for the original month, the clawback is the **full difference in commission** for that month — not just the one client
- Same-month cancels are **never** a clawback (commission was never paid)

### Draw vs Commission
- The agent's hourly pay for the 1st–15th of the month acts as a **draw**
- If commission > draw → agent receives commission only
- If commission < draw → agent keeps the draw, **no repayment required**

---

## CSV Format (Manual Upload)

If you want to manually enter data instead of uploading a CRM file, the manual CSV requires these columns:

```
agent_name, units_cleared, total_cleared_debt, cancellation_rate, hourly_draw, period
```

- `cancellation_rate`: percentage as a number (e.g. `18.5` = 18.5%)
- `period`: format `YYYY-MM` (e.g. `2026-05`)

Download a sample file from the Upload page.

---

## CRM Export Columns Required

The CRM upload requires these columns (others are optional but stored):

```
Sales Rep, 1st Payment Cleared Date, Dropped Date, Status, Enrolled Debt, # NSF
```

A unit is **cleared** when:
- `1st Payment Cleared Date` has a date
- `Dropped Date` is empty
- `Status` is not "Pending Affiliate Cancellation"

A unit is **cancelled** when `Dropped Date` has a date.

---

## Exporting Results

- **Period export**: Click "Export CSV" on any results page to download all agents for that period
- **Agent export**: Click "Export Agent CSV" on any agent detail page to download that agent's full client list — useful to share with the agent directly

---

## History

Click **History** in the top navigation to see all previously uploaded periods. You can view or export any past period from there.

To re-upload a period (e.g. with updated data), you must delete it first from the results page, then re-upload.
