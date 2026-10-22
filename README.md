# Bark lead automation

This program watches a Bark seller dashboard and adds new architectural and structural leads to a Google Sheet. It keeps running and checks Bark every 5 minutes.

On each check it:

1. Signs in to Bark and reads the leads currently on the dashboard.
2. Appends only leads that are not already on the sheet. The lead's name goes in the Name column. The full lead text is stored as a note on the Description cell.
3. Gives each new lead a daily number in the No column. Numbers use the America/Chicago clock. The first lead after midnight is 0, and the numbers continue through 40 (41 Bark seller accounts). Any further lead that day keeps an empty No cell.
4. For a lead numbered 0 through 40, signs in to that seller account and clicks the blue Contact button. It never clicks Buy credits. If Bark asks for credits, the lead stays on the sheet and the buy button is left alone. Leads with an empty No are not contacted.
5. When a new Bark posting matches an older row, inserts one row using that existing name plus `(exist)`, copies the older note, and leaves No empty so Contact is not clicked. This happens once, not on every check.
6. Reads new mail from `team@bark.com` in one Gmail inbox, `spearbig1021@gmail.com`. Forward the Bark mail from the other seller inboxes into that account. When a new message arrives, the program writes that full name into the one sheet row that is still waiting for it. If the email name is the same as the name already in the cell, the cell becomes `Lonnie (*)`. A full name that already belongs to a lead in another city is not copied onto a different lead.

The first time the program runs, it only records the leads already on Bark. It does not fill the sheet with that existing list.

Only one copy of the program can run at a time. A second copy stops with "A Bark sync is already running."

## What you need

- Windows
- Python 3.11
- Google Chrome, for the Gmail window
- A Google Cloud service account JSON key
- The Google Sheet shared with that service account as an Editor

The sheet needs these columns: Name, No, Description, Created, Updated, Phone, and Email.

## Setup

From the project folder:

```bat
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
set PLAYWRIGHT_BROWSERS_PATH=%CD%\.playwright
.\.venv\Scripts\python.exe -m playwright install chromium
```

Create a `.env` file in the project folder:

```env
BARK_EMAIL=seller@example.com
BARK_PASSWORD=your-bark-password
BARK_LOGIN_URL=https://www.bark.com/en/gb/login/
BARK_LEADS_URL=https://www.bark.com/sellers/dashboard/
GOOGLE_SHEET_ID=your-spreadsheet-id
GOOGLE_SHEET_GID=0
GOOGLE_CREDENTIALS_PATH=credentials.json
POLL_INTERVAL_MINUTES=5
HEADLESS=true
```

`BARK_EMAIL` and `BARK_PASSWORD` are the dashboard account used to read the lead list. Put the service-account key file at `credentials.json`.

Create `accounts.json` with one Bark seller login for each No from 0 through 40. The program uses that login when it clicks Contact:

```json
[
  {"no": 0, "email": "seller0@example.com", "password": "secret"},
  {"no": 1, "email": "seller1@example.com", "password": "secret"}
]
```

Create `gmail_accounts.json` for the inboxes that receive Bark's full-name mail. Every full-name message should be forwarded to `spearbig1021@gmail.com`. That is the only inbox the program reads:

```json
[
  {
    "email": "spearbig1021@gmail.com",
    "password": "secret",
    "recovery": "recovery@example.com",
    "nos": [0, 37, 38, 39]
  }
]
```

These files stay on this computer. They are listed in `.gitignore` and must not be committed:

- `.env`
- `credentials.json`
- `accounts.json`
- `gmail_accounts.json`
- `sessions/`
- `sync.log`

## Run

Leave the full-name Gmail window open before starting the sync. Close any other Chrome window first. This starts Chrome with remote debugging so the program can read new Bark subjects every 5 minutes without signing in again:

```bat
open_gmail_chrome.bat
```

Sign in to `spearbig1021@gmail.com` in that window once, and leave it open.

Start the sync:

```bat
run.bat
```

Or run it directly:

```bat
.\.venv\Scripts\python.exe -u main.py
```

Useful options:

```bat
.\.venv\Scripts\python.exe -u main.py --once
.\.venv\Scripts\python.exe -u main.py --dry-run
.\.venv\Scripts\python.exe -u main.py --notes-only
```

- `--once` runs a single check and then exits.
- `--dry-run` reads Bark and prints leads. It does not write to the sheet.
- `--notes-only` refreshes description notes and does not append leads.

While it is running, progress is printed and also written to `sync.log`.
