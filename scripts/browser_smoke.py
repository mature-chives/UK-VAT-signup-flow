from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.async_api import async_playwright

from vat_automation.config import PageRule, Settings
from vat_automation.runner import VatAutomation


async def run() -> None:
    settings = Settings(
        start_url="about:blank",
        profile_dir=Path("/private/tmp/uk-vat-smoke-profile"),
        artifacts_dir=Path("/private/tmp/uk-vat-smoke-artifacts"),
        answers={
            "Choose one": "true",
            "Conditional details": "synthetic detail",
            "Country": "China",
        },
        pages=[PageRule(path_contains="about:blank")],
        headless=True,
    )
    runner = VatAutomation(settings, interactive=False)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.set_content(
            """
            <main><form>
              <h1>Smoke test</h1>
              <fieldset><legend>Choose one</legend>
                <input id="yes" name="choice" type="radio" value="true"
                  onchange="document.querySelector('#conditional').hidden=false">
                <label for="yes">Yes</label>
                <input id="no" name="choice" type="radio" value="false">
                <label for="no">No</label>
              </fieldset>
              <div id="conditional" hidden>
                <label for="details">Conditional details</label>
                <input id="details" name="details" type="text">
              </div>
              <label for="country">Country</label>
              <input id="country" name="country" role="combobox" aria-autocomplete="list"
                oninput="document.querySelector('#china').hidden=false">
              <div id="china" role="option" hidden
                onclick="document.body.dataset.countrySelected='true'">China</div>
              <button>Continue</button>
            </form></main>
            """
        )
        missing = await runner._fill_until_stable(page, "Smoke test")
        assert not missing, missing
        assert await page.locator("#yes").is_checked()
        assert await page.locator("#details").input_value() == "synthetic detail"
        assert await page.locator("body").get_attribute("data-country-selected") == "true"
        controls = await runner._controls(page)
        assert any(control.value == "true" for control in controls)

        await page.set_content(
            """
            <main><ul>
              <li>Completed task <strong>Completed</strong><a href="#done">Done</a></li>
              <li>Next task <strong>Not started</strong><a href="#next">Next</a></li>
            </ul></main>
            """
        )
        assert await runner._click_next_task(page)
        assert page.url.endswith("#next")

        os.environ["HMRC_MFA_METHOD"] = "Text message"
        await page.set_content(
            """
            <main><form>
              <h1>Add a way to get access codes</h1>
              <input id="app" name="method" type="radio" value="app">
              <label for="app">Authenticator app for smartphone or tablet</label>
              <input id="call" name="method" type="radio" value="call">
              <label for="call">Phone call</label>
              <input id="text" name="method" type="radio" value="text">
              <label for="text">Text message</label>
              <button type="button"
                onclick="document.body.dataset.mfaContinued='true'">Continue</button>
            </form></main>
            """
        )
        await runner._handle_auth(page, "Add a way to get access codes")
        assert await page.locator("#text").is_checked()
        assert await page.locator("body").get_attribute("data-mfa-continued") == "true"

        await page.set_content(
            """
            <main><form>
              <h1>Are you adding a UK mobile number?</h1>
              <input id="uk-yes" name="uk-number" type="radio" value="true">
              <label for="uk-yes">Yes</label>
              <input id="uk-no" name="uk-number" type="radio" value="false">
              <label for="uk-no">No</label>
              <button type="button"
                onclick="document.body.dataset.ukContinued='true'">Continue</button>
            </form></main>
            """
        )
        await runner._handle_auth(page, "Are you adding a UK mobile number?")
        assert await page.locator("#uk-yes").is_checked()
        assert await page.locator("body").get_attribute("data-uk-continued") == "true"

        await page.set_content(
            """
            <main><form>
              <h1>Enter a country for this mobile phone number</h1>
              <label for="mobile-country">Enter the country for this mobile phone number</label>
              <input id="mobile-country" name="country" type="text">
              <button type="button"
                onclick="document.body.dataset.countryContinued='true'">Continue</button>
            </form></main>
            """
        )
        await runner._handle_auth(
            page, "Enter a country for this mobile phone number"
        )
        assert await page.locator("#mobile-country").input_value() == "China"
        assert (
            await page.locator("body").get_attribute("data-country-continued")
            == "true"
        )

        os.environ["HMRC_MFA_PHONE"] = "00000000000"
        await page.set_content(
            """
            <main><form>
              <h1>Enter a mobile phone number</h1>
              <label for="mobileNumber">Mobile phone number</label>
              <input id="mobileNumber" name="mobileNumber" type="tel">
              <button type="button"
                onclick="document.body.dataset.codeSent='true'">Send access code</button>
            </form></main>
            """
        )
        await runner._handle_auth(page, "Enter a mobile phone number")
        assert await page.locator("#mobileNumber").input_value() == "00000000000"
        assert await page.locator("body").get_attribute("data-code-sent") == "true"

        await page.set_content(
            """
            <main><form>
              <h1>Enter the access code</h1>
              <label for="oneTimePassword">Access code</label>
              <input id="oneTimePassword" name="oneTimePassword" type="tel">
              <button>Continue</button>
            </form></main>
            """
        )
        try:
            await runner._handle_auth(page, "Enter the access code")
        except Exception as exc:
            assert "验证码页面需要用户输入验证码" in str(exc), exc
        else:
            raise AssertionError("oneTimePassword should pause for verification input")

        await page.set_content(
            """
            <main>
              <h1>Manage your VAT registration applications</h1>
              <table><tbody>
                <tr><th>Reference</th><th>Status</th></tr>
                <tr><td><a href="#draft">No reference</a></td><td>Draft</td></tr>
              </tbody></table>
              <a href="#create">Create a new application</a>
            </main>
            """
        )
        assert await runner._click_create_vat_application(page)
        assert page.url.endswith("#create")

        await browser.close()
    print("browser-smoke: ok")


if __name__ == "__main__":
    asyncio.run(run())
