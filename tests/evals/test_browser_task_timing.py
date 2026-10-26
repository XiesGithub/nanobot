"""Actual browser event-clock controls for the V073 evidence collector."""
import sys
from pathlib import Path

from evals import browser_task_driver as driver
from evals.browser_task_diagnostics import collect_v073


def test_trusted_click_and_two_frame_final_dom_are_distinct_from_action_return(tmp_path):
    cached = Path(driver.__file__).resolve().parents[1] / ".tmp/browser-python"
    if str(cached) not in sys.path:
        sys.path.insert(0, str(cached))
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(driver._browser_executable()))
        context = browser.new_context()
        context.add_init_script(driver.V073_TIMING_SCRIPT)
        page = context.new_page()
        try:
            with collect_v073(page, tmp_path, "timing-control"):
                page.goto("data:text/html,<main></main><textarea aria-label='Message input'></textarea>"
                          "<button aria-label='Send message'>Send</button>")
                page.evaluate("""() => {
                  document.querySelector('button').onclick = () => {
                    const input = document.querySelector('textarea'); input.disabled = true;
                    setTimeout(() => {
                      document.querySelector('main').innerHTML = '<div><div>5</div><div><button aria-label="Copy reply">Copy</button></div></div>';
                      input.disabled = false;
                    }, 50);
                  };
                }""")
                calibration = {"samples": [driver._v073_clock_sample(page)]}
                empty = driver._v073_timing(page, calibration)
                assert empty["click"] is None and empty["final_dom"] is None
                page.get_by_role("button", name="Send message", exact=True).click()
                page.wait_for_function("!!window.__nanobotV073Timing.final_dom")
                observed = driver._v073_timing(page, calibration)
                assert observed["click"]["is_trusted"] is True
                assert observed["click"]["target"] == "Send message"
                final = observed["final_dom"]
                assert final["text"] == "5" and final["visible"]
                assert final["stable_frames"] == 2
                assert observed["click"]["performance_ms"] < final["performance_ms"] < final["confirmed_performance_ms"]
                sample = calibration["samples"][0]
                assert sample["host_before_monotonic_ns"] <= sample["host_after_monotonic_ns"]
        finally:
            browser.close()


def test_programmatic_click_is_retained_as_untrusted_not_promoted(tmp_path):
    cached = Path(driver.__file__).resolve().parents[1] / ".tmp/browser-python"
    if str(cached) not in sys.path:
        sys.path.insert(0, str(cached))
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(driver._browser_executable()))
        context = browser.new_context()
        context.add_init_script(driver.V073_TIMING_SCRIPT)
        page = context.new_page()
        try:
            with collect_v073(page, tmp_path, "timing-control"):
                page.goto("data:text/html,<button aria-label='Send message'>Send</button>")
                page.evaluate("document.querySelector('button').click()")
                evidence = driver._v073_timing(page, {"samples": []})
                assert evidence["click"]["is_trusted"] is False
                assert evidence["final_dom"] is None
        finally:
            browser.close()
