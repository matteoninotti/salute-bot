"""Live Playwright drive of the CUP no-login JSF flow (Phase 4).

The real `Scraper` behind the seam (`base.py`). Drives headless Chromium through the
stateful Liferay/ICEfaces flow: a **real browser** harvests `ViewState` / `p_auth` /
`ice.*` itself, so we never replay raw POSTs (D18; recon discovery 3 — replaying would
mean re-harvesting and re-injecting every token at every step).

Flow — verified against `recon/flow.har` + the public form DOM:
    GET  cup.isan.csi.it/                          seed cookies
    GET  .../web/guest/ricetta-dematerializzata    form (CFInput + nreInput0)
    click the VISIBLE "Avanti" (`nreButton` div; the real submit is the hidden
        `epPrestazioniForwardNavigate` input) ..... epPrestazioni.xhtml (confirmation)
    click "Avanti" (`epPrestazioni-nextButton-main`) appuntamentiPrimaDisp.xhtml
    leave the geo selectors empty, click "Estendi area di ricerca" (`nextArea`) so the
        search always reaches the WIDEST area before harvesting (D17) ...............
        the `availableAppointmentsContainer` partial-response with the slots

Outcomes map to the seam's types (`base.py`): a parsed prestazione + slots →
`ScrapeResult`; the permanent invalid/expired/consumed-ricetta banner → `NREInvalidError`
(D28 rotation — confirmed live from a dead ricetta: "Impossibile recuperare la ricetta
dematerializzata", a page-level banner, NOT the field-level error spans); any other
flow/timeout/parse failure → `ScrapeError` (transient, D11). The proceed→confirmation
step races the banner against the confirmation so a dead ricetta fails fast. Parsing
reuses the offline-tested `parse_prestazione_confirmation` / `parse_available_slots`.
"""

import os
import re
import sys
import time
from collections.abc import Mapping

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from salutebot.scraper.base import NREInvalidError, ScrapeError, ScrapeResult
from salutebot.scraper.confirmation import parse_prestazione_confirmation
from salutebot.scraper.parser import parse_available_slots

_SEED_URL = "https://cup.isan.csi.it/"
_FORM_URL = "https://cup.isan.csi.it/web/guest/ricetta-dematerializzata"

# JSF element ids (confirmed from the public form DOM + the two redacted captures).
# The portlet prefix is constant; the real submit inputs are display:none, so we click
# the VISIBLE proceed buttons (recon §1) and let ICEfaces fire the hidden submit.
_P = "_ricettaelettronica_WAR_cupprenotazione_:"
_CF_INPUT = _P + "ePrescriptionSearchForm:CFInput"
_NRE_INPUT = _P + "ePrescriptionSearchForm:nreInput0"        # single NRE box (box-count = 1)
_SEARCH_BTN = _P + "ePrescriptionSearchForm:nreButton"       # visible "Avanti" div
_NRE_ERROR = _P + "ePrescriptionSearchForm:nreError0"        # invalid-NRE hook (SMOKE-CONFIRM)
_CF_ERROR = _P + "ePrescriptionSearchForm:cfError"
_CONFIRM_NEXT = _P + "navigation-epPrestazioni-main:epPrestazioni-nextButton-main"  # "Avanti"
_APPUNTAMENTI_FORM = _P + "appuntamentiForm"                 # the slots page is up
_NEXT_AREA = _P + "appuntamentiForm:nextArea"               # "Estendi area di ricerca"
_WARNING_CONFIRM = _P + "appuntamentiForm:dialogAppuntamentiWarningConfirmButton"
_SLOTS_CONTAINER = _P + "appuntamentiForm:availableAppointmentsContainer"
_CONFIRM_ROW = ".prestazioneRow"                             # confirmation parse target (D14)
# The permanent invalid/expired/consumed-ricetta signal (D28), captured live from a
# known-dead ricetta: a page-level banner "Impossibile recuperare la ricetta
# dematerializzata" (NOT the field-level nreError0/cfError spans). Matched by text.
_INVALID_RICETTA_RE = re.compile(r"impossibile recuperare la ricetta", re.I)
# "altre disponibilità" is a positional j_idt button (id renumbers per render, HAR
# source `_t385`), so it is matched by its visible label, not its id (SMOKE-CONFIRM).
_MORE_AVAIL_RE = re.compile(r"altre disponibilit", re.I)
_SLOT_CARD = "div.disponibiliPanel"                          # one slot card (what the parser reads)
# Short settle between the two proceed clicks — long enough for the ICEfaces input
# validation to finish, short enough not to reintroduce the old ~30 s wait.
_PROCEED_SETTLE_MS = 5000
# The post-extend warning dialog is usually ABSENT; when it appears it does so at once.
# So poll it only briefly (a full action-window wait here just burns ~10 s per scrape).
_DIALOG_WAIT_MS = 2000


def _sel(element_id: str) -> str:
    """CSS attribute selector for a JSF id (colons make `#id` unusable)."""
    return f'[id="{element_id}"]'


class LiveScraper:
    """Drives the CUP flow for one `(CF, NRE)` via headless Chromium (D5/D18).

    Attributes private (encapsulation guardrail). A fresh browser + context is used per
    scrape, so each run is a clean session (matches D4: tokens/cookies re-harvested every
    cycle, never persisted) with no state leaking between scrapes."""

    def __init__(self, headless: bool = True, timeout_s: float = 60.0,
                 action_s: float = 10.0, debug: bool = False) -> None:
        self.__headless = headless
        # The CUP site is slow (the slots page can take ~15 s), so the page-transition
        # timeout is generous.
        self.__timeout_ms = int(timeout_s * 1000)
        # Shorter wait for CONDITIONAL controls (the extend-area buttons, the warning
        # dialog, slot cards): present on some ricette, absent on others — don't burn the
        # full timeout when they legitimately aren't there.
        self.__action_ms = int(action_s * 1000)
        self.__debug = debug  # opt-in step-by-step diagnostics to stderr (no secrets)
        self.__t0 = 0.0       # flow-start monotonic clock, for timed debug lines
        self.__t_last = 0.0   # previous debug line's timestamp (for the per-step delta)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "LiveScraper":
        """Build from env: `SALUTEBOT_HEADFUL` (truthy → show the browser);
        `SALUTEBOT_SCRAPE_TIMEOUT` (seconds, default 60); `SALUTEBOT_DEBUG` (truthy →
        print flow diagnostics to stderr — button clicks + card counts, never secrets)."""
        src = os.environ if env is None else env
        headful = (src.get("SALUTEBOT_HEADFUL", "").strip().lower() in ("1", "true", "yes"))
        debug = (src.get("SALUTEBOT_DEBUG", "").strip().lower() in ("1", "true", "yes"))
        timeout = float(src.get("SALUTEBOT_SCRAPE_TIMEOUT", "60"))
        return cls(headless=not headful, timeout_s=timeout, debug=debug)

    def __log(self, message: str) -> None:
        """Print a timed diagnostic line when `debug` is on (opt-in; labels + counts, no
        secrets). Each line shows total elapsed since flow start and Δ since the last line,
        so slow steps are visible in the output itself."""
        if not self.__debug:
            return
        now = time.monotonic()
        total = now - self.__t0
        delta = now - self.__t_last
        self.__t_last = now
        print(f"[debug] [{total:6.1f}s  Δ{delta:5.1f}s]  {message}", file=sys.stderr)

    def scrape(self, cf: str, nre: str) -> ScrapeResult:
        """Run the full flow once and return the prestazione + current slots.

        Any browser/flow error becomes a transient `ScrapeError` (D11) so the daemon
        retries rather than crashing on the fragile JSF flow. Secrets are typed into the
        page but never logged; error text carries only the exception type, never a value."""
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=self.__headless)
                try:
                    page = browser.new_context().new_page()
                    page.set_default_timeout(self.__timeout_ms)
                    return self.__run_flow(page, cf, nre)
                finally:
                    browser.close()
        except NREInvalidError:
            raise  # permanent (D28) — let it propagate for rotation
        except ScrapeError:
            raise  # already the transient type
        except Exception as exc:  # any Playwright/other failure → transient (D11)
            raise ScrapeError(f"live drive failed: {type(exc).__name__}") from exc

    # ----- flow steps (each grounded in the HAR; SMOKE-CONFIRM where dynamic) -----

    def __run_flow(self, page, cf: str, nre: str) -> ScrapeResult:
        self.__t0 = self.__t_last = time.monotonic()          # start the debug clock
        page.goto(_SEED_URL, wait_until="domcontentloaded")   # seed session cookies
        page.goto(_FORM_URL, wait_until="domcontentloaded")   # the CF+NRE form
        self.__log("form loaded")

        page.fill(_sel(_CF_INPUT), cf)
        page.fill(_sel(_NRE_INPUT), nre)
        self.__proceed_to_confirmation(page)                  # raises NREInvalidError on a dead ricetta

        prestazione = parse_prestazione_confirmation(page.content())
        if prestazione is None:
            # Could be an invalid NRE OR a real flow glitch — treated as transient until
            # the invalid signal is wired (D28 / 2026-07-04 decision). SMOKE-CONFIRM.
            raise ScrapeError("confirmation page had no parseable prestazione")
        self.__log(f"confirmation reached: {prestazione.code}")

        page.click(_sel(_CONFIRM_NEXT))                       # "Avanti" → appuntamenti page
        page.wait_for_selector(_sel(_APPUNTAMENTI_FORM))      # the slots page is up
        self.__log("appuntamenti page loaded")
        self.__extend_to_widest_area(page)                    # D17
        self.__await_slot_cards(page)                         # let the slots render (0 is valid)

        container_html = page.locator(_sel(_SLOTS_CONTAINER)).inner_html()
        slots = parse_available_slots(container_html)         # may be [] (valid "no slots")
        self.__log(f"slot cards in container: {self.__card_count(page)}; parsed slots: {len(slots)}")
        return ScrapeResult(prestazione=prestazione, slots=slots)

    def __proceed_to_confirmation(self, page) -> None:
        """Advance the CF+NRE form to the epPrestazioni confirmation.

        The proceed needs **two** clicks (recon + confirmed live): the 1st runs an
        ICEfaces input-validation pass, the 2nd advances. Clicking twice — with the
        validation allowed to settle between — avoids waiting out a full timeout after an
        otherwise-ineffective single click. If the confirmation never appears, any text in
        the `nreError0`/`cfError` spans is surfaced in the error (the likely invalid-NRE
        signal, D28 — captured for free by a dead-ricetta smoke run)."""
        page.click(_sel(_SEARCH_BTN))                         # 1st: input-validation pass
        self.__settle(page, _PROCEED_SETTLE_MS)               # let the validation ajax finish
        self.__click_if_present(page, page.locator(_sel(_SEARCH_BTN)))  # 2nd: advance (if still there)
        self.__await_confirmation_or_invalid(page)

    def __await_confirmation_or_invalid(self, page) -> None:
        """Race the confirmation against the invalid-ricetta banner (D28), so a dead ricetta
        fails **fast** with the permanent `NREInvalidError` rather than waiting out the full
        timeout. Whichever appears first wins; if neither does, it's a transient failure."""
        deadline = time.monotonic() + self.__timeout_ms / 1000
        while time.monotonic() < deadline:
            if page.locator(_CONFIRM_ROW).count() > 0:
                return                                        # advanced to the confirmation
            if page.get_by_text(_INVALID_RICETTA_RE).count() > 0:
                self.__log("invalid-ricetta banner detected (D28)")
                raise NREInvalidError("ricetta dematerializzata non recuperabile "
                                      "(scaduta, già utilizzata o non valida)")
            page.wait_for_timeout(400)
        spans = self.__error_spans(page)
        raise ScrapeError(
            "confirmation never appeared" + (f" — page said: {spans}" if spans else "")
        )

    def __error_spans(self, page) -> str:
        """Combined visible text of the form's validation spans (never a CF/NRE value)."""
        parts = []
        for element_id in (_NRE_ERROR, _CF_ERROR):
            try:
                text = page.locator(_sel(element_id)).inner_text(timeout=1000).strip()
            except Exception:
                text = ""
            if text:
                parts.append(text)
        return " | ".join(parts)

    def __extend_to_widest_area(self, page) -> None:
        """Always advance to the widest search area before harvesting (D17).

        The geo selectors are left empty (they submit NO_VALUE); reaching the wide list is
        a **two-step, conditional** branch (recon; HAR sources `_t385` → `nextArea`): first
        "altre disponibilità", then "Estendi area di ricerca". Both are best-effort — a
        ricetta with near-home availability may show neither — so each is clicked only if
        present, then the ICEfaces partial-response is allowed to settle. An optional
        warning dialog is confirmed if it appears. SMOKE-CONFIRM: the exact labels + waits."""
        more = self.__click_if_present(page, page.get_by_text(_MORE_AVAIL_RE))
        self.__log(f"'altre disponibilità': {'clicked' if more else 'not found'}")
        extended = self.__click_if_present(page, page.locator(_sel(_NEXT_AREA)))
        self.__log(f"'estendi area' (nextArea): {'clicked' if extended else 'not found'}")
        if extended:
            dialog = self.__click_if_present(page, page.locator(_sel(_WARNING_CONFIRM)),
                                             timeout_ms=_DIALOG_WAIT_MS)
            self.__log(f"warning dialog: {'confirmed' if dialog else 'none'}")

    def __await_slot_cards(self, page) -> None:
        """Wait for slot cards to render after the extend (they arrive via a partial-
        response). 0 cards is a valid outcome, so a timeout here is NOT an error — the
        container is read regardless."""
        try:
            page.wait_for_selector(f"{_sel(_SLOTS_CONTAINER)} {_SLOT_CARD}",
                                   timeout=self.__action_ms)
        except PlaywrightTimeoutError:
            pass

    def __card_count(self, page) -> int:
        return page.locator(f"{_sel(_SLOTS_CONTAINER)} {_SLOT_CARD}").count()

    def __click_if_present(self, page, locator, timeout_ms: int | None = None) -> bool:
        """Click the first match if it becomes visible within the given window (default the
        action window); return whether it was clicked. Used for the conditional
        proceed/extend-area/dialog controls."""
        target = locator.first
        try:
            target.wait_for(state="visible", timeout=timeout_ms or self.__action_ms)
        except PlaywrightTimeoutError:
            return False
        target.click()
        return True

    def __settle(self, page, timeout_ms: int | None = None) -> None:
        """Let the ICEfaces partial-response finish. Best-effort: the app keeps a poll
        channel open, so networkidle may not fire — the caller reads on regardless."""
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms or self.__timeout_ms)
        except PlaywrightTimeoutError:
            pass

def _smoke(argv: list[str] | None = None) -> None:
    """Live smoke run (D-Phase-4): one real scrape, secrets from env or a no-echo prompt,
    printing only the resolved prestazione + slot summary (never the CF/NRE).

    Usage: `SALUTEBOT_SMOKE_CF=... SALUTEBOT_SMOKE_NRE=... python -m salutebot.scraper.drive`
    (or run bare and type them at the prompts). Prompts are **visible** — this is a local
    dev tool, and a prompt is not shell history / argv / a log, so it keeps D35's actual
    guarantees. `SALUTEBOT_HEADFUL=1` shows the browser so you can watch the flow."""
    from salutebot.validation import validate_cf, validate_nre

    cf_raw = os.environ.get("SALUTEBOT_SMOKE_CF") or input("Codice Fiscale: ")
    nre_raw = os.environ.get("SALUTEBOT_SMOKE_NRE") or input("NRE (codice completo di 15 caratteri, es. 010A3 + 10 cifre): ")
    try:
        cf, nre = validate_cf(cf_raw), validate_nre(nre_raw)
    except ValueError as err:
        print(f"input rifiutato: {err}")
        return

    print("avvio del flusso CUP (headless) — può richiedere qualche secondo...")
    try:
        result = LiveScraper.from_env().scrape(cf, nre)
    except NREInvalidError as err:
        print(f"ricetta non valida (D28, permanente): {err}")
        return
    except ScrapeError as err:
        print(f"scraping fallito (temporaneo): {err}")
        return

    p = result.prestazione
    print(f"\nprestazione: {p.descrizione} ({p.code})  quantità={p.quantita}")
    print(f"posti trovati: {len(result.slots)}")
    for slot in result.slots[:5]:
        print(f"  {slot.iso_date} {slot.time} — {slot.struttura} [{slot.cap}]")
    if len(result.slots) > 5:
        print(f"  … e altri {len(result.slots) - 5}")


if __name__ == "__main__":
    _smoke()
