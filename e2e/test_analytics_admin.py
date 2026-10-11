"""Rendered Analytics and Free Models tabs in the local Admin UI."""

import pytest
from playwright.sync_api import Page, expect


def _open_analytics(page: Page, admin_base_url: str) -> None:
    page.goto(f"{admin_base_url}/admin")
    expect(page.locator("#messageArea")).to_have_text("")
    page.get_by_role("button", name="Analytics", exact=True).click()
    # Wait for the first poll to land before asserting anything.
    expect(page.locator("#analyticsKpis")).not_to_be_empty()


def _open_free_models(page: Page, admin_base_url: str) -> None:
    page.goto(f"{admin_base_url}/admin")
    expect(page.locator("#messageArea")).to_have_text("")
    page.get_by_role("button", name="Free Models", exact=True).click()
    expect(page.locator("#freeGuardSummary")).not_to_be_empty()


def test_analytics_tab_shows_empty_state_and_metadata_note(
    page: Page, admin_base_url: str
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_analytics(page, admin_base_url)
    expect(page.locator("#pageTitle")).to_have_text("Analytics")
    # With no traffic yet the tab must say so rather than invent zero values.
    expect(page.locator("#analyticsRequests")).to_contain_text(
        "No recorded requests", timeout=15000
    )
    expect(page.locator("#analyticsChart")).to_contain_text("No requests in range")
    # The accounting semantics must be stated, not implied.
    expect(page.locator("#view-analytics")).to_contain_text(
        "one client request may produce several downstream attempts",
        ignore_case=True,
    )
    expect(page.locator("#view-analytics")).to_contain_text(
        "never as zero", ignore_case=True
    )


def test_analytics_refresh_and_trace_controls_render(
    page: Page, admin_base_url: str
) -> None:
    _open_analytics(page, admin_base_url)
    expect(page.get_by_role("button", name="Refresh", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Export", exact=True)).to_be_visible()
    expect(page.locator("#analyticsRange")).to_have_value("24h")
    expect(page.locator("#analyticsIncludeProbes")).not_to_be_checked()
    # Stat tiles are dl/dt/dd pairs, one per measured quantity.
    expect(page.locator("#analyticsKpis .analytics-stat").first).to_be_visible()
    expect(page.locator("#analyticsKpis")).to_contain_text("Logical requests")
    expect(page.locator("#analyticsKpis")).to_contain_text("Guard-blocked")
    expect(page.locator("#analyticsKpis")).to_contain_text("Unknown token share")


def test_free_models_tab_explains_why_models_are_blocked(
    page: Page, admin_base_url: str
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    _open_free_models(page, admin_base_url)
    expect(page.locator("#pageTitle")).to_have_text("Free Models")
    # The "zero price is not proof" promise must be visible, not implied.
    expect(page.locator("#view-free-models")).to_contain_text(
        "a zero price does not prove a model is free without a card", ignore_case=True
    )
    expect(
        page.get_by_role("button", name="Refresh prices & eligibility")
    ).to_be_visible()
    expect(page.locator("#freeGuardSummary")).to_contain_text("Mode")
    expect(page.locator("#freeGuardSummary")).to_contain_text("Verified free")
    expect(page.locator("#freeGuardSummary")).to_contain_text("Unknown")


def test_free_models_tab_lists_blocked_models_with_attestation(
    page: Page, admin_base_url: str
) -> None:
    _open_free_models(page, admin_base_url)
    expect(page.locator("#freeGuardSummary")).not_to_be_empty()
    rows = page.locator("#freeGuardModels tbody tr")
    expect(page.locator("#freeGuardModels")).to_contain_text("No eligibility evidence")
    if rows.count():
        expect(rows.first).to_contain_text("Attest free")


@pytest.mark.parametrize("width", [1440, 1280, 900])
def test_new_tabs_are_usable_at_common_widths(
    page: Page, admin_base_url: str, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    _open_analytics(page, admin_base_url)
    expect(page.locator("#analyticsKpis .analytics-stat").first).to_be_visible()
    _open_free_models(page, admin_base_url)
    expect(page.locator("#freeGuardSummary .analytics-stat").first).to_be_visible()


def test_new_tabs_share_the_admin_design_language(
    page: Page, admin_base_url: str
) -> None:
    """Both tabs must render inside the same settings-section card as other pages."""

    page.set_viewport_size({"width": 1440, "height": 900})
    _open_analytics(page, admin_base_url)
    card = page.locator("#view-analytics section.settings-section")
    expect(card).to_have_count(1)
    expect(card.locator(".section-heading h3")).to_have_text("Analytics")
    # Dark theme tokens are applied, so stat text is legible rather than white-on-white.
    background = page.locator("#analyticsKpis .analytics-stat").first.evaluate(
        "node => getComputedStyle(node).backgroundColor"
    )
    assert background == "rgb(21, 24, 34)"  # --card
    label = page.locator("#analyticsKpis dt").first.evaluate(
        "node => getComputedStyle(node).color"
    )
    assert label == "rgb(156, 163, 175)"  # --muted
