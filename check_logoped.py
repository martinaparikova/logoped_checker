import os
import re
import smtplib
from urllib.parse import urljoin
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PWTimeoutError
from playwright.sync_api import sync_playwright


@dataclass(frozen=True)
class Slot:
    slot_id: str
    label: str


STATE_PATH = Path("storage_state.json")
SEEN_PATH = Path("seen_slots.txt")
DOTENV_PATH = Path(".env")


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Chybí proměnná prostředí {name}. Viz README.md.")
    return v


def _parse_recipients(raw: str) -> list[str]:
    # podpora: "a@b.cz,b@c.cz" / "a@b.cz; b@c.cz" / s mezerami a novými řádky
    parts = re.split(r"[,\n;]+", raw)
    return [p.strip() for p in parts if p.strip()]


def send_mail(subject: str, body: str, attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    host = _env("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = _env("SMTP_USER")
    pw = _env("SMTP_PASS")
    recipients = _parse_recipients(_env("MAIL_TO"))
    if not recipients:
        raise RuntimeError("MAIL_TO je prázdné.")

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(body)

    for filename, data, mime in (attachments or []):
        maintype, subtype = (mime.split("/", 1) + ["octet-stream"])[:2]
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    def _send_with(client) -> None:
        client.login(user, pw)
        client.send_message(msg, to_addrs=recipients)

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port) as s:
                _send_with(s)
        else:
            with smtplib.SMTP(host, port) as s:
                s.starttls()
                _send_with(s)
    except smtplib.SMTPAuthenticationError as e:
        # Gmail typicky vyžaduje "app password" (ne běžné heslo) pokud je zapnuté 2FA.
        raise RuntimeError(
            "SMTP přihlášení selhalo. Pokud používáte Gmail, nastavte do SMTP_PASS "
            "App Password (ne běžné heslo) a ujistěte se, že máte zapnuté 2FA."
        ) from e


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    return {x.strip() for x in SEEN_PATH.read_text(encoding="utf-8").splitlines() if x.strip()}


def save_seen(items: Iterable[str]) -> None:
    SEEN_PATH.write_text("\n".join(sorted(set(items))), encoding="utf-8")


def ensure_logged_in(page, url: str, user: str, pw: str) -> None:
    """
    SuperSaaS někdy zobrazí login jen s heslem (uživatel je zapamatovaný),
    nebo naopak plný login (uživatel + heslo).

    Proto se umíme přihlásit přes oba typy formuláře.
    """
    page.goto(url, wait_until="domcontentloaded")

    # Pokud nejde o login stránku, jsme buď přihlášení, nebo to chce JS.
    if page.get_by_text("Přihlášení klient").count() == 0 and page.get_by_text("Prosím, přihlaste se").count() == 0:
        return

    # Pokud jsme na variantě loginu jen s heslem, bývá dole odkaz
    # "Přihlaste se s registrovaným uživatelským jménem" -> přepne na plný login.
    switch_link = page.get_by_role("link", name=re.compile(r"uživatelským\s+jménem", re.I))
    if switch_link.count() > 0:
        href = switch_link.first.get_attribute("href")
        if href:
            page.goto(urljoin(page.url, href), wait_until="domcontentloaded")

    # Heslo (musí být vždy).
    pass_field = None
    for c in (
        page.locator("input#password"),
        page.locator("input[name='password']"),
        page.get_by_label(re.compile(r"heslo", re.I)),
        page.locator("input[type='password']"),
    ):
        if c.count() > 0:
            pass_field = c.first
            break
    if not pass_field:
        raise RuntimeError("Nepodařilo se najít pole pro heslo.")

    # Uživatelské jméno je někdy schované (viz váš screenshot) – pak ho nevyplňujeme.
    user_field = None
    user_candidates = [
        page.locator("input#name"),
        page.locator("input[name='name']"),
        page.get_by_label(re.compile(r"uživatelské\s+jm[eé]no", re.I)),
        page.get_by_label(re.compile(r"přihlašovací\s+jm[eé]no", re.I)),
        page.get_by_label(re.compile(r"e-?mail", re.I)),
        page.locator("input[id='name']"),
        page.locator("input[name='login']"),
        page.locator("input[id='login']"),
        page.locator("input[name*='login' i]"),
        page.locator("input[name*='user' i]"),
        page.locator("input[id*='user' i]"),
        page.locator("form input[type='text']"),
    ]
    for c in user_candidates:
        if c.count() > 0:
            user_field = c.first
            break

    if user_field:
        try:
            user_field.fill(user)
        except Exception:
            # kdyby to byl špatný text input, ignorujeme a zkusíme login jen heslem
            user_field = None
    else:
        # Pokud jsme stále na loginu bez user fieldu, zkusíme ještě jednou natvrdo "role=user"
        # (někdy je href relativní / přesměrování).
        if "role=user" not in page.url:
            forced = re.sub(r"[?&]role=[^&]+", "", page.url)
            sep = "&" if "?" in forced else "?"
            page.goto(f"{forced}{sep}role=user", wait_until="domcontentloaded")
            # re-detekce user fieldu po přepnutí
            for c in user_candidates:
                if c.count() > 0:
                    user_field = c.first
                    break
            if user_field:
                user_field.fill(user)

    # Pokud jsme na "role=user" login stránce a user field stále nemáme,
    # radši skončíme s jasnou chybou (jinak prohlížeč odmítne submit validací).
    if "role=user" in page.url and not user_field:
        raise RuntimeError("Na přihlašovací stránce chybí pole pro uživatelské jméno.")

    pass_field.fill(pw)

    # Tlačítko "Přihlásit se"
    btn = page.get_by_role("button", name=re.compile(r"přihlásit", re.I))
    if btn.count() == 0:
        btn = page.get_by_role("button").first
    btn.click()
    page.wait_for_load_state("networkidle")


def login_and_open_schedule(page, login_url: str, schedule_url: str, user: str, pw: str) -> None:
    # Vždy začneme "správnou" login stránkou (s user+pass), pokud je dostupná.
    page.goto(login_url, wait_until="domcontentloaded")
    if page.get_by_text("Přihlášení klient").count() > 0 or page.get_by_text("Prosím, přihlaste se").count() > 0:
        ensure_logged_in(page, login_url, user, pw)

    # Pak přejdeme na samotný rozvrh.
    page.goto(schedule_url, wait_until="domcontentloaded")
    if page.get_by_text("Přihlášení klient").count() > 0 or page.get_by_text("Prosím, přihlaste se").count() > 0:
        ensure_logged_in(page, schedule_url, user, pw)


def wait_schedule_render(page) -> None:
    # HTML má prázdný #viewholder, JS ho naplní.
    page.wait_for_selector("#viewholder", state="attached", timeout=15000)
    try:
        page.wait_for_function(
            "() => document.querySelector('#viewholder') && document.querySelector('#viewholder').children.length > 0",
            timeout=15000,
        )
    except PWTimeoutError:
        # Někdy je vyplněný až vnořeně (bez přímých children), tak zkusíme aspoň viditelný grid.
        page.wait_for_timeout(2000)


def _slot_id_from_element(el) -> str:
    # Preferujeme URL/onclick, aby to bylo stabilní pro "seen" deduplikaci.
    href = el.get_attribute("href") or ""
    if href:
        return href
    onclick = el.get_attribute("onclick") or ""
    if onclick:
        return onclick
    # Fallback: data-* nebo text
    for attr in ("data-id", "data-reservation-id", "data-time", "title"):
        v = el.get_attribute(attr) or ""
        if v:
            return f"{attr}={v}"
    return (el.inner_text() or "").strip()


def find_available_slots(page) -> list[Slot]:
    """
    SuperSaaS volné termíny obvykle reprezentuje jako klikací "prázdnou" buňku v kalendáři,
    často přes onclick na vytvoření nové rezervace / zobrazení dialogu 'reservation'.

    Použijeme několik selektorů a výsledky deduplikujeme.
    """
    wait_schedule_render(page)

    candidates = []

    # 1) Klikací prvky, které otevírají dialog "reservation" / volají newRes apod.
    candidates.append(page.locator("[onclick*='reservation']", has_text=""))
    candidates.append(page.locator("[onclick*='newRes']"))
    candidates.append(page.locator("[onclick*='showDialog'][onclick*='reservation']"))

    # 2) Odkazy, které vypadají jako vytvoření rezervace (rezervovat / vytvořit rezervaci)
    candidates.append(page.get_by_role("link", name=re.compile(r"rezerv", re.I)))
    candidates.append(page.get_by_role("button", name=re.compile(r"vytvoř", re.I)))

    # 3) Ikona plus v gridu (viz váš screenshot)
    candidates.append(page.locator("img[alt='+'], img[alt*='plus' i], img[src*='plus' i]"))

    found: dict[str, str] = {}

    for loc in candidates:
        try:
            n = min(loc.count(), 2000)  # pojistka
        except Exception:
            continue

        for i in range(n):
            el = loc.nth(i)

            # Pokud je to IMG, zkus najít nejbližší klikací rodič.
            tag = ""
            try:
                tag = (el.evaluate("e => e.tagName") or "").lower()
            except Exception:
                pass
            if tag == "img":
                parent_link = el.locator("xpath=ancestor::a[1]")
                parent_btn = el.locator("xpath=ancestor::button[1]")
                if parent_link.count() > 0:
                    el = parent_link.first
                elif parent_btn.count() > 0:
                    el = parent_btn.first

            slot_id = _slot_id_from_element(el)
            if not slot_id:
                continue

            label = ""
            try:
                label = (el.inner_text() or "").strip()
            except Exception:
                label = ""
            if not label:
                label = slot_id

            # Odfiltruj věci typu logout/setting apod.
            if "logout" in slot_id or "change" in slot_id:
                continue

            found.setdefault(slot_id, label)

    # Heuristika: pokud není nic nalezeno, zkusíme ještě najít "Klikněte na prázdné místo..."
    # (to značí, že UI umožňuje vytvářet, jen není volno v aktuálním pohledu).
    return [Slot(slot_id=k, label=v) for k, v in found.items()]


def maybe_write_debug(page) -> None:
    if os.environ.get("DEBUG_SHOT") != "1":
        return
    # Zapiš aspoň URL a title vždy, i když content/screenshot selže.
    meta = ""
    try:
        meta = f"<!-- url={page.url} title={page.title()!r} -->\n"
    except Exception:
        meta = "<!-- meta unavailable -->\n"
    try:
        page.screenshot(path="debug.png", full_page=True)
    except Exception:
        pass
    try:
        Path("debug.html").write_text(meta + page.content(), encoding="utf-8")
    except Exception as e:
        Path("debug.html").write_text(
            meta + f"<!-- debug fallback; page.content() failed: {e!r} -->\n",
            encoding="utf-8",
        )


def main() -> None:
    # override=True je důležité: pokud jste si dřív nastavila proměnné v PowerShellu,
    # bez toho by se dál používaly staré hodnoty (hlavně SMTP_PASS).
    load_dotenv(dotenv_path=DOTENV_PATH, override=True)

    tz_name = os.environ.get("TZ", "Europe/Prague")
    active_hours = os.environ.get("ACTIVE_HOURS", "7-23")
    m = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", active_hours)
    if not m:
        raise RuntimeError("ACTIVE_HOURS musí být ve formátu '7-23'.")
    start_h, end_h = int(m.group(1)), int(m.group(2))
    now_local = datetime.now(ZoneInfo(tz_name))
    if not (start_h <= now_local.hour < end_h):
        return

    url = _env("SS_URL")
    login_url = os.environ.get("SS_LOGIN_URL") or "https://www.supersaas.cz/schedule/logoradotin?role=user"
    user = _env("SS_USER")
    pw = _env("SS_PASS")

    seen = load_seen()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE_PATH) if STATE_PATH.exists() else None)
        page = context.new_page()

        try:
            login_and_open_schedule(page, login_url, url, user, pw)
            wait_schedule_render(page)
            maybe_write_debug(page)
        except Exception:
            # Když to spadne dřív (typicky timeout / přesměrování na login),
            # chceme mít vždy snapshot HTML pro diagnostiku.
            try:
                maybe_write_debug(page)
            finally:
                raise

        # Ulož session pro příště (ať se nemusí logovat pokaždé, když to jde).
        context.storage_state(path=str(STATE_PATH))

        slots = find_available_slots(page)

        new_slots = [s for s in slots if s.slot_id not in seen]
        if new_slots:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            lines = "\n".join([f"- {s.label} ({s.slot_id})" for s in new_slots])
            attachments: list[tuple[str, bytes, str]] = []
            if os.environ.get("EMAIL_SCREENSHOT", "1") == "1":
                try:
                    png = page.screenshot(full_page=True)
                    attachments.append(("logoped_slots.png", png, "image/png"))
                except Exception:
                    pass
            send_mail(
                subject=f"[Logoped] Nový volný termín ({len(new_slots)})",
                body=f"Nalezeny nové možné volné sloty v {now}:\n\n{lines}\n\nStránka: {url}\n",
                attachments=attachments,
            )
            save_seen(seen | {s.slot_id for s in new_slots})

        context.close()
        browser.close()


if __name__ == "__main__":
    main()

