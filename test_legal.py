"""Launch-readiness guards: paid legal docs, public offer, /paysupport, CI.

These lock in the P0 legal + CI hardening so the product can no longer drift
back into "we charge money under free-beta terms" or a workflow that never runs.
"""
from pathlib import Path

import bot

ROOT = Path(__file__).resolve().parent


def _read(rel):
    return (ROOT / rel).read_text(encoding='utf-8')


# ── Legal documents reflect the PAID state, not a free closed beta ──
def test_terms_no_longer_advertise_free_beta():
    terms = _read('miniapp/terms.html')
    assert 'beta' not in terms.lower()
    assert 'закрите тестування' not in terms
    # Real paid terms + link to the governing offer.
    assert '199' in terms
    assert '/offer' in terms


def test_privacy_no_longer_mentions_beta():
    privacy = _read('miniapp/privacy.html')
    assert 'beta' not in privacy.lower()
    assert 'закрите тестування' not in privacy


def test_public_offer_exists_with_core_clauses():
    offer = _read('miniapp/offer.html')
    # Seller identification (legally required for a public offer).
    assert 'Бурмич Сергій Володимирович' in offer
    assert '3250824537' in offer  # ІПН
    # Commercial essentials.
    assert '199' in offer
    assert '14 днів' in offer
    assert 'повернення' in offer.lower()
    # Liability limitation for tax outcomes — every council flagged this.
    assert 'штраф' in offer.lower()
    assert 'не несе відповідальності' in offer


# ── Static server publishes the offer at /offer ──
def test_server_serves_offer_route():
    server = _read('miniapp/server.py')
    assert "add_get('/offer'" in server
    assert "add_get('/offer.html'" in server
    assert "_legal_document('offer.html'" in server


# ── Bot surfaces offer + /paysupport ──
def test_bot_registers_offer_and_paysupport_commands():
    assert hasattr(bot, 'offer_command')
    assert hasattr(bot, 'paysupport_command')
    src = _read('bot.py')
    assert 'CommandHandler("offer"' in src
    assert 'CommandHandler("paysupport"' in src


def test_support_contact_has_nonempty_default():
    assert isinstance(bot.SUPPORT_CONTACT, str) and bot.SUPPORT_CONTACT


def test_paywall_text_points_to_offer_and_paysupport():
    text = bot._paywall_bot_text({'trial_eligible': False})
    assert '/offer' in text
    assert '/paysupport' in text


# ── Strengthened tax disclaimer takes responsibility off the provider ──
def test_tax_disclaimer_disclaims_penalties():
    assert 'ДПС' in bot.TAX_DISCLAIMER or 'бухгалтер' in bot.TAX_DISCLAIMER


# ── CI regression guard: the runner-context bug must stay fixed ──
def test_ci_workflow_has_no_runner_context_in_job_env():
    ci = _read('.github/workflows/ci.yml')
    # `${{ runner.* }}` is unavailable in job-level env and rejects the WHOLE
    # workflow ("Unrecognized named-value: 'runner'"), so pytest never runs.
    assert 'runner.temp' not in ci
