# CrispCheckerV3.py

import csv
import time
import asyncio
import logging
import argparse
from pathlib import Path
from playwright.async_api import async_playwright, Playwright, Browser, Page, TimeoutError

# Default values - these can now be overridden by command-line arguments
DEFAULT_INPUT_FILE = "websites.csv"
DEFAULT_CSV_OUTPUT_FILE = 'crisp_check_results.csv'
DEFAULT_HTML_OUTPUT_FILE = 'crisp_dashboard.html'
DEFAULT_CONCURRENT_CHECKS = 5

# Default timeout values (in milliseconds)
DEFAULT_PAGE_LOAD_TIMEOUT_MS = 30000
DEFAULT_JS_CHECK_TIMEOUT_MS = 10000
DEFAULT_NETWORK_IDLE_TIMEOUT_MS = 15000
DEFAULT_CHAT_BUTTON_CLICK_TIMEOUT_MS = 5000
DEFAULT_POST_CLICK_WAIT_MS = 3000

# Configure logging (basic setup, level will be set by argparse)
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger()  # This logger is for backend console logs


# MODIFIED: check_crisp now accepts and uses configurable timeouts
async def check_crisp(
        url: str,
        browser: Browser,
        page_load_timeout_ms: int = DEFAULT_PAGE_LOAD_TIMEOUT_MS,
        js_check_timeout_ms: int = DEFAULT_JS_CHECK_TIMEOUT_MS,
        network_idle_timeout_ms: int = DEFAULT_NETWORK_IDLE_TIMEOUT_MS,
        chat_button_click_timeout_ms: int = DEFAULT_CHAT_BUTTON_CLICK_TIMEOUT_MS,  # Added
        post_click_wait_ms: int = DEFAULT_POST_CLICK_WAIT_MS  # Added
) -> tuple[str, str, list[str]]:
    """
    Checks a given URL for Crisp integration, collects logs, and returns status, error, and logs.
    Uses configurable timeouts.

    Args:
        url: The URL of the website to check.
        browser: An existing Playwright Browser instance to use.
        page_load_timeout_ms: Timeout for page.goto().
        js_check_timeout_ms: Timeout for page.wait_for_function().
        network_idle_timeout_ms: Timeout for page.wait_for_load_state('networkidle').
        chat_button_click_timeout_ms: Timeout for clicking the chat button.
        post_click_wait_ms: Time to wait after a chat button click.

    Returns:
        A tuple containing:
            - status ('Uses Crisp', 'Does NOT use Crisp', 'Error')
            - error message (empty string if no error)
            - log_messages (a list of formatted log strings for this URL)
    """
    page: Page = None
    crisp_detection_state = {"found": False}
    error_msg_final = ""
    status_final = "Error"

    log_messages = []

    def _capture_log(level: str, message: str):
        timestamp = time.strftime("%H:%M:%S", time.localtime())
        log_entry = f"[{timestamp}] {level.upper()}: {message}"
        log_messages.append(log_entry)
        # getattr(logger, level.lower())(f"({url}) {message}") # Optionally log to backend console too

    _capture_log("info",
                 f"Starting check for {url} with PageTO:{page_load_timeout_ms}, JSTO:{js_check_timeout_ms}, IdleTO:{network_idle_timeout_ms}")

    async def handle_response(response):
        try:
            if "crisp.chat" in response.url:
                _capture_log("info", f"Crisp found in network URL: {response.url}")
                crisp_detection_state["found"] = True
                return

            if 200 <= response.status <= 299:
                try:
                    response_content = await response.text()
                    if "crisp.chat" in response_content or "CRISP_WEBSITE_ID" in response_content:
                        _capture_log("info", f"Crisp found in network content from: {response.url}")
                        crisp_detection_state["found"] = True
                        return
                except Exception:
                    pass
        except Exception as e:
            _capture_log("error", f"Error during response handling for {response.url}: {e}")

    try:
        page = await browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1280, 'height': 720}
        )
        _capture_log("debug", "Page created.")

        page.on("response", handle_response)
        _capture_log("debug", "Response event listener attached.")

        await page.route("**/*", lambda route: (
            route.abort()
            if route.request.resource_type in ["image", "stylesheet", "font", "media", "manifest", "other"]
            else route.continue_()
        ))
        _capture_log("debug", "Aggressive resource blocking enabled.")

        try:
            _capture_log("info", f"Navigating to {url}")
            # Use configurable page_load_timeout_ms
            await page.goto(url, timeout=page_load_timeout_ms, wait_until='domcontentloaded')
            _capture_log("info", "DOMContentLoaded reached.")

            if crisp_detection_state["found"]:
                _capture_log("info", "Crisp found via network interception (early).")
                status_final, error_msg_final = "Uses Crisp", ""
                return status_final, error_msg_final, log_messages

            try:
                _capture_log("debug", "Trying to click chat button.")
                chat_button_locator = page.locator(
                    "div[class*='chat'], button[class*='chat'], [data-crisp-id], #crisp-chatbox")
                if await chat_button_locator.count() > 0:
                    # Use configurable chat_button_click_timeout_ms
                    await chat_button_locator.first.click(timeout=chat_button_click_timeout_ms)
                    _capture_log("info", "Chat button clicked.")
                    # Use configurable post_click_wait_ms
                    await page.wait_for_timeout(post_click_wait_ms)
                else:
                    _capture_log("info", "No obvious chat button found.")
            except TimeoutError:
                _capture_log("warning",
                             f"Timeout ({chat_button_click_timeout_ms}ms) while clicking chat button. Continuing...")
            except Exception as e:
                _capture_log("warning", f"Error clicking chat button: {e}. Continuing...")

            if crisp_detection_state["found"]:
                _capture_log("info", "Crisp found via network interception (post-click).")
                status_final, error_msg_final = "Uses Crisp", ""
                return status_final, error_msg_final, log_messages

            _capture_log("debug", "Waiting for Crisp JS objects.")
            try:
                # Use configurable js_check_timeout_ms
                await page.wait_for_function(
                    "() => typeof $crisp !== 'undefined' || typeof CRISP_WEBSITE_ID !== 'undefined'",
                    timeout=js_check_timeout_ms
                )
                _capture_log("info", "Crisp found via JavaScript evaluation.")
                status_final, error_msg_final = "Uses Crisp", ""
                return status_final, error_msg_final, log_messages
            except TimeoutError:
                _capture_log("info",
                             f"Crisp JS objects not found via wait_for_function (timeout: {js_check_timeout_ms}ms).")

            _capture_log("debug", "Waiting for network idle (fallback).")
            # Use configurable network_idle_timeout_ms
            await page.wait_for_load_state('networkidle', timeout=network_idle_timeout_ms)
            _capture_log("info", "Network idle state reached (or timeout).")

            crisp_in_js_fallback = await page.evaluate("""
                () => { return typeof $crisp !== 'undefined' || typeof CRISP_WEBSITE_ID !== 'undefined'; }
            """)
            if crisp_in_js_fallback:
                _capture_log("info", "Crisp found via JS evaluation (fallback).")
                status_final, error_msg_final = "Uses Crisp", ""
                return status_final, error_msg_final, log_messages

            _capture_log("debug", "Checking HTML content.")
            content = await page.content()
            if "crisp.chat" in content or "CRISP_WEBSITE_ID" in content:
                _capture_log("info", "Crisp found in HTML content.")
                status_final, error_msg_final = "Uses Crisp", ""
                return status_final, error_msg_final, log_messages

            if crisp_detection_state["found"]:
                _capture_log("info", "Crisp found via network interception (final check).")
                status_final, error_msg_final = "Uses Crisp", ""
                return status_final, error_msg_final, log_messages

            _capture_log("info", "Crisp NOT found after all checks.")
            status_final, error_msg_final = "Does NOT use Crisp", ""
            return status_final, error_msg_final, log_messages

        except TimeoutError as e:
            error_msg_final = f"Timeout: {e}"
            _capture_log("error", error_msg_final)
            status_final = "Error"
            if crisp_detection_state["found"]: status_final = "Uses Crisp"
            return status_final, error_msg_final, log_messages
        except Exception as e:
            error_msg_final = str(e)
            if "net::ERR_NAME_NOT_RESOLVED" in error_msg_final: error_msg_final = "Network Error: DNS Resolution Failed"
            # ... (other specific error messages) ...
            _capture_log("error", f"General error: {error_msg_final}")
            status_final = "Error"
            if crisp_detection_state["found"]: status_final = "Uses Crisp"
            return status_final, error_msg_final, log_messages

    except Exception as e_outer:
        error_msg_final = f"Critical error in check_crisp setup: {str(e_outer)}"
        _capture_log("critical", error_msg_final)
        status_final = "Error"
        if crisp_detection_state["found"]: status_final = "Uses Crisp"
        return status_final, error_msg_final, log_messages
    finally:
        if page:
            try:
                await page.close()
                _capture_log("debug", "Page closed.")
            except Exception as e_close:
                _capture_log("error", f"Error closing page: {e_close}")
        _capture_log("info", f"Finished check for {url}. Status: {status_final}")


# Function to read website URLs from a CSV file (remains the same)
def read_websites(file_path):
    logger.info(f"  > Reading websites from {file_path}")
    input_path = Path(file_path).resolve()
    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        return []
    with open(input_path, newline='', encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile)
        websites = []
        for i, row in enumerate(reader):
            url_value = row.get('url') or row.get(reader.fieldnames[0]) if reader.fieldnames else None
            if url_value:
                websites.append(url_value.strip())
        logger.info(f"  > Found {len(websites)} websites.")
        return websites


# Function to write results to a CSV file (remains the same)
def write_csv(results, output_file):
    fieldnames = ['url', 'status', 'error']
    with open(output_file, mode='w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow({'url': row['url'], 'status': row['status'], 'error': row['error']})
    logger.info(f"Results saved to {output_file}")


# Function to write results to an HTML dashboard (remains the same structure)
def write_html(results, output_file):
    uses_crisp_count = sum(1 for r in results if r['status'] == 'Uses Crisp')
    not_uses_crisp_count = sum(1 for r in results if r['status'] == 'Does NOT use Crisp')
    error_count = sum(1 for r in results if r['status'] == 'Error')
    total_count = len(results)

    html_content = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Crisp Check Dashboard</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-primary: #fafbfc; --bg-secondary: #ffffff; --bg-tertiary: #f8fafc;
            --text-primary: #1a202c; --text-secondary: #4a5568; --text-muted: #718096;
            --border-light: #e2e8f0; --border-medium: #cbd5e0;
            --accent-blue: #3182ce; --accent-blue-light: #bee3f8;
            --success: #38a169; --success-light: #c6f6d5;
            --error: #e53e3e; --error-light: #fed7d7;
            --warning: #d69e2e; --warning-light: #faf089;
            --shadow-sm: 0 1px 3px 0 rgba(0,0,0,0.1), 0 1px 2px 0 rgba(0,0,0,0.06);
            --shadow-md: 0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -1px rgba(0,0,0,0.06);
            --radius-lg: 12px; --radius-md: 8px;
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Inter', sans-serif; background: var(--bg-primary); color: var(--text-primary); line-height: 1.6; font-size: 14px; padding: 24px; }}
        .container {{ max-width: 1200px; margin: 0 auto; display: flex; flex-direction: column; gap: 24px; }}
        .header {{ text-align: center; margin-bottom: 8px; }}
        .header h1 {{ font-size: 32px; font-weight: 700; color: var(--text-primary); margin-bottom: 8px; }}
        .header .subtitle {{ color: var(--text-muted); font-size: 16px; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; margin-bottom: 8px; }}
        .stat-card {{ background: var(--bg-secondary); border: 1px solid var(--border-light); border-radius: var(--radius-lg); padding: 24px; text-align: center; box-shadow: var(--shadow-sm); position: relative; }}
        .stat-card::before {{ content: ''; position: absolute; top: 0; left: 0; right: 0; height: 4px; border-radius: var(--radius-lg) var(--radius-lg) 0 0; }}
        .stat-card.total::before {{ background: linear-gradient(90deg, var(--accent-blue), #4299e1); }}
        .stat-card.success::before {{ background: linear-gradient(90deg, var(--success), #48bb78); }}
        .stat-card.error::before {{ background: linear-gradient(90deg, var(--error), #f56565); }}
        .stat-card.warning::before {{ background: linear-gradient(90deg, var(--warning), #ecc94b); }}
        .stat-number {{ font-size: 36px; font-weight: 700; margin-bottom: 8px; }}
        .stat-card.total .stat-number {{ color: var(--accent-blue); }}
        .stat-card.success .stat-number {{ color: var(--success); }}
        .stat-card.error .stat-number {{ color: var(--error); }}
        .stat-card.warning .stat-number {{ color: var(--warning); }}
        .stat-label {{ font-size: 14px; color: var(--text-secondary); font-weight: 500; text-transform: uppercase; }}
        .controls {{ background: var(--bg-secondary); border: 1px solid var(--border-light); border-radius: var(--radius-lg); padding: 20px 24px; box-shadow: var(--shadow-sm); display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 16px; }}
        .search-input {{ flex: 1; padding: 10px 16px; border: 1px solid var(--border-medium); border-radius: var(--radius-md); font-size: 14px; background: var(--bg-tertiary); }}
        .search-input:focus {{ border-color: var(--accent-blue); background: var(--bg-secondary); box-shadow: 0 0 0 3px var(--accent-blue-light); }}
        .filter-select {{ padding: 10px 16px; border: 1px solid var(--border-medium); border-radius: var(--radius-md); background: var(--bg-secondary); font-size: 14px; cursor: pointer; min-width: 160px; }}
        .table-container {{ background: var(--bg-secondary); border: 1px solid var(--border-light); border-radius: var(--radius-lg); overflow: hidden; box-shadow: var(--shadow-sm); }}
        .table-wrapper {{ overflow-x: auto; }}
        table {{ width: 100%; border-collapse: collapse; }}
        thead {{ background: var(--bg-tertiary); }}
        th {{ padding: 16px 20px; text-align: left; font-weight: 600; font-size: 12px; color: var(--text-secondary); text-transform: uppercase; border-bottom: 2px solid var(--border-light); }}
        tbody tr {{ border-bottom: 1px solid var(--border-light); }}
        tbody tr:hover {{ background: var(--bg-tertiary); }}
        td {{ padding: 16px 20px; vertical-align: middle; }}
        .url-link {{ color: var(--accent-blue); text-decoration: none; font-weight: 500; display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 300px; }}
        .status-badge {{ display: inline-flex; padding: 6px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
        .status-badge.uses {{ background: var(--success-light); color: var(--success); }}
        .status-badge.not-uses {{ background: var(--error-light); color: var(--error); }}
        .status-badge.error {{ background: var(--warning-light); color: var(--warning); }}
        .error-cell {{ color: var(--text-muted); font-size: 13px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .hidden {{ display: none !important; }}
        .empty-state {{ text-align: center; padding: 48px 20px; color: var(--text-muted); }}
        @media (max-width: 768px) {{ .stats-grid {{ grid-template-columns: repeat(2, 1fr); }} .controls {{ flex-direction: column; align-items: stretch; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Crisp Integration Dashboard</h1>
            <p class="subtitle">Website analysis results</p>
        </div>
        <div class="stats-grid">
            <div class="stat-card total"><div class="stat-number">{total_count}</div><div class="stat-label">Total Checked</div></div>
            <div class="stat-card success"><div class="stat-number">{uses_crisp_count}</div><div class="stat-label">Uses Crisp</div></div>
            <div class="stat-card error"><div class="stat-number">{not_uses_crisp_count}</div><div class="stat-label">No Crisp Found</div></div>
            <div class="stat-card warning"><div class="stat-number">{error_count}</div><div class="stat-label">Check Errors</div></div>
        </div>
        <div class="controls">
            <input type="text" id="searchInput" class="search-input" placeholder="Search websites...">
            <select id="statusFilter" class="filter-select">
                <option value="all">All Results</option>
                <option value="uses">Uses Crisp</option>
                <option value="not-uses">No Crisp</option>
                <option value="error">Errors</option>
            </select>
        </div>
        <div class="table-container"><div class="table-wrapper">
            <table>
                <thead><tr><th>Website</th><th>Status</th><th>Error Details</th></tr></thead>
                <tbody id="resultsTable">
    """
    for row in results:
        status_class = ''
        if row['status'] == 'Uses Crisp':
            status_class = 'uses'
        elif row['status'] == 'Does NOT use Crisp':
            status_class = 'not-uses'
        else:
            status_class = 'error'
        html_content += f"""
                    <tr class="{status_class}" data-url="{row['url'].lower()}">
                        <td><a href="{row['url']}" target="_blank" class="url-link" title="{row['url']}">{row['url']}</a></td>
                        <td><span class="status-badge {status_class}">{row['status']}</span></td>
                        <td class="error-cell" title="{row['error']}">{row['error']}</td>
                    </tr>"""
    html_content += """
                </tbody></table>
            <div id="emptyState" class="empty-state hidden"><p>No results found.</p></div>
        </div></div></div>
    <script>
        const searchInput = document.getElementById('searchInput');
        const statusFilter = document.getElementById('statusFilter');
        const resultsTableBody = document.getElementById('resultsTable'); 
        const emptyState = document.getElementById('emptyState');
        function filterTable() {
            const searchTerm = searchInput.value.toLowerCase();
            const filterValue = statusFilter.value;
            const rows = resultsTableBody.getElementsByTagName('tr');
            let visibleCount = 0;
            for (let i = 0; i < rows.length; i++) {
                const row = rows[i];
                const url = (row.getAttribute('data-url') || '').toLowerCase(); 
                const matchesSearch = url.includes(searchTerm);
                const matchesFilter = filterValue === 'all' || row.classList.contains(filterValue);
                if (matchesSearch && matchesFilter) {
                    row.classList.remove('hidden');
                    visibleCount++;
                } else {
                    row.classList.add('hidden');
                }
            }
            emptyState.classList.toggle('hidden', visibleCount > 0);
        }
        searchInput.addEventListener('input', filterTable);
        statusFilter.addEventListener('change', filterTable);
        filterTable(); // Initial call
    </script>
</body></html>
"""
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    logger.info(f"HTML dashboard saved to {output_file}")


# Main function for standalone script execution
async def main():
    script_start_time = time.monotonic()

    parser = argparse.ArgumentParser(description="Crisp Chat Detector for Websites")
    parser.add_argument("-i", "--input", type=str, default=DEFAULT_INPUT_FILE,
                        help=f"Input CSV file (default: {DEFAULT_INPUT_FILE})")
    parser.add_argument("-c", "--csv-output", type=str, default=DEFAULT_CSV_OUTPUT_FILE,
                        help=f"CSV output file (default: {DEFAULT_CSV_OUTPUT_FILE})")
    parser.add_argument("-H", "--html-output", type=str, default=DEFAULT_HTML_OUTPUT_FILE,
                        help=f"HTML dashboard file (default: {DEFAULT_HTML_OUTPUT_FILE})")
    parser.add_argument("-j", "--jobs", type=int, default=DEFAULT_CONCURRENT_CHECKS,
                        help=f"Concurrent checks (default: {DEFAULT_CONCURRENT_CHECKS})")
    parser.add_argument("--headful", action="store_true", help="Run in headful mode (show browser UI)")
    parser.add_argument("-l", "--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging level (default: INFO)")

    # Add arguments for timeouts for standalone script
    parser.add_argument("--page-timeout", type=int, default=DEFAULT_PAGE_LOAD_TIMEOUT_MS,
                        help=f"Page load timeout in ms (default: {DEFAULT_PAGE_LOAD_TIMEOUT_MS})")
    parser.add_argument("--js-timeout", type=int, default=DEFAULT_JS_CHECK_TIMEOUT_MS,
                        help=f"JavaScript check timeout in ms (default: {DEFAULT_JS_CHECK_TIMEOUT_MS})")
    parser.add_argument("--idle-timeout", type=int, default=DEFAULT_NETWORK_IDLE_TIMEOUT_MS,
                        help=f"Network idle timeout in ms (default: {DEFAULT_NETWORK_IDLE_TIMEOUT_MS})")
    parser.add_argument("--click-timeout", type=int, default=DEFAULT_CHAT_BUTTON_CLICK_TIMEOUT_MS,
                        help=f"Chat button click timeout in ms (default: {DEFAULT_CHAT_BUTTON_CLICK_TIMEOUT_MS})")
    parser.add_argument("--post-click-wait", type=int, default=DEFAULT_POST_CLICK_WAIT_MS,
                        help=f"Wait after click in ms (default: {DEFAULT_POST_CLICK_WAIT_MS})")

    args = parser.parse_args()

    logger.setLevel(args.log_level.upper())
    logger.info("Starting CrispChecker script (standalone mode)...")
    websites = read_websites(args.input)
    results_data = []

    if not websites:
        logger.warning("No websites found. Exiting.")
        script_end_time = time.monotonic()
        duration = script_end_time - script_start_time
        logger.info(f"Script finished in {duration:.2f} seconds (no websites to process).")
        return

    async with async_playwright() as p_main:
        browser_main: Browser = await p_main.chromium.launch(
            headless=not args.headful,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas', '--no-first-run', '--no-zygote',
                '--single-process', '--disable-gpu'
            ]
        )
        logger.info(f"Browser launched in standalone mode (headless: {not args.headful}).")

        page_creation_limiter = asyncio.Semaphore(args.jobs)
        tasks = []

        async def run_check_with_limiter(current_url, p_browser):
            async with page_creation_limiter:
                # Pass timeout arguments from command line to check_crisp
                status, error, logs = await check_crisp(
                    current_url,
                    p_browser,
                    page_load_timeout_ms=args.page_timeout,
                    js_check_timeout_ms=args.js_timeout,
                    network_idle_timeout_ms=args.idle_timeout,
                    chat_button_click_timeout_ms=args.click_timeout,
                    post_click_wait_ms=args.post_click_wait
                )
                return {'url': current_url, 'status': status, 'error': error, 'logs': logs}

        for url_item in websites:
            tasks.append(run_check_with_limiter(url_item, browser_main))

        checked_results_list_of_dicts = await asyncio.gather(*tasks)

        await browser_main.close()
        logger.info("Browser closed in standalone mode.")

    for res_dict in checked_results_list_of_dicts:
        results_data.append(res_dict)
        log_line_for_console = f"{res_dict['url']} -> {res_dict['status']}"
        if res_dict['error']:
            log_line_for_console += f" (Error: {res_dict['error']})"
        logger.info(log_line_for_console)

    write_csv(results_data, args.csv_output)
    write_html(results_data, args.html_output)

    logger.info(f"\n✅ Results saved to {args.csv_output} and {args.html_output}")

    script_end_time = time.monotonic()
    duration = script_end_time - script_start_time
    logger.info(f"CrispChecker script finished in {duration:.2f} seconds.")


if __name__ == '__main__':
    asyncio.run(main())
