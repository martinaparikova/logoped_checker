# Logoped checker (SuperSaaS)

Lokální skript, který každých X minut zkontroluje, jestli se na SuperSaaS stránce objevil volný termín, a když ano, pošle e‑mail.

## Instalace

```bash
cd C:\Users\marti\logoped_checker
pip install -r requirements.txt
python -m playwright install
```

## Nastavení proměnných prostředí (PowerShell)

```powershell
$env:SS_URL="https://www.supersaas.cz/schedule/logoradotin/zanetadostalova?view=free"
$env:SS_USER="vas_login"   # vaše uživatelské jméno pro SuperSaaS
$env:SS_PASS="vase_heslo"

$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="vasmail@gmail.com"
$env:SMTP_PASS="app_heslo" # u Gmailu App Password (ne běžné heslo)
$env:MAIL_TO="kam_to_poslat@gmail.com"
```

## Alternativa: soubor `.env` (doporučeno)

Vytvořte soubor `C:\Users\marti\logoped_checker\.env` a vložte do něj:

```text
SS_URL=https://www.supersaas.cz/schedule/logoradotin/zanetadostalova?view=free
SS_LOGIN_URL=https://www.supersaas.cz/schedule/logoradotin?role=user
SS_USER=vas_login
SS_PASS=vase_heslo

SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=vasmail@gmail.com
SMTP_PASS=app_heslo
MAIL_TO=kam_to_poslat@gmail.com,manzel@gmail.com
```

Pozn.: u Gmailu je potřeba **App Password** (vyžaduje zapnuté 2FA). Pokud dáte běžné heslo,
Gmail vrátí chybu typu `SMTPAuthenticationError (534) Application-specific password required`.
V tom případě vytvořte App Password v nastavení Google účtu a vložte ho do `SMTP_PASS`.

Pak už stačí jen spustit `python check_logoped.py` (skript si `.env` načte automaticky).

## Co přijde v e‑mailu

- Výpis nalezených slotů
- Přiložený screenshot stránky (`logoped_slots.png`)

Pokud nechcete screenshot, nastavte `EMAIL_SCREENSHOT=0` v `.env`.

## Ruční spuštění

```bash
python check_logoped.py
```

Tip: když chcete ladit selektory, nastavte:

```powershell
$env:DEBUG_SHOT="1"
```

Skript pak uloží `debug.png` a `debug.html` do stejné složky.

## Spouštění každých 10 minut (Windows – Plánovač úloh)

Vytvořte úlohu:

- Spouštěč: Opakovat úlohu každých **10 minut**
- Akce: Spustit program
  - Program/script: `python`
  - Přidat argumenty: `C:\Users\marti\logoped_checker\check_logoped.py`
  - Spustit v: `C:\Users\marti\logoped_checker`

## GitHub Actions (běh každých 10 minut)

Workflow je v `.github/workflows/logoped-check.yml`.

1) Vytvořte nový (ideálně privátní) GitHub repo a nahrajte do něj obsah složky `logoped_checker` (bez `.env`).

2) V GitHub repo nastavte **Settings → Secrets and variables → Actions → New repository secret** a přidejte:

- `SS_URL` (např. `https://www.supersaas.cz/schedule/logoradotin/zanetadostalova?view=free`)
- `SS_LOGIN_URL` (např. `https://www.supersaas.cz/schedule/logoradotin?role=user`)
- `SS_USER`
- `SS_PASS`
- `SMTP_HOST` (např. `smtp.gmail.com`)
- `SMTP_PORT` (doporučeno `465` pro Gmail)
- `SMTP_USER`
- `SMTP_PASS` (Gmail App Password)
- `MAIL_TO`

3) Hotovo. Workflow poběží každých 10 minut, ale skript je omezený na **07:00–23:00 Europe/Prague**.

