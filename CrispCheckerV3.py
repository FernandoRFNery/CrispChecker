import csv
import time
import asyncio
import logging
import argparse
from pathlib import Path
from playwright.async_api import async_playwright, Playwright, Browser, Page
from playwright.sync_api import TimeoutError


# Default values - these can now be overridden by command-line arguments
DEFAULT_INPUT_FILE = "websites.csv"
DEFAULT_CSV_OUTPUT_FILE = 'crisp_check_results.csv'
DEFAULT_HTML_OUTPUT_FILE = 'crisp_dashboard.html'
DEFAULT_CONCURRENT_CHECKS = 5

# Configure logging (basic setup, level will be set by argparse)
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger()


async def check_crisp(url: str, semaphore: asyncio.Semaphore, headless_mode: bool) -> tuple[str, str]:
    """
    Checks a given URL for Crisp integration asynchronously.
    Includes more aggressive resource blocking and targeted waiting strategies for performance.

    Args:
        url: The URL of the website to check.
        semaphore: An asyncio.Semaphore to limit concurrent access to the browser.
        headless_mode: Boolean indicating whether to run Playwright in headless mode.

    Returns:
        A tuple containing the status ('Uses Crisp', 'Does NOT use Crisp', 'Error')
        and an error message (empty string if no error).
    """
    async with semaphore:
        crisp_found_in_network = False
        error_msg = "" # Initialize error_msg

        async with async_playwright() as p:
            browser: Browser = await p.chromium.launch(
                headless=headless_mode,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--single-process',
                    '--disable-gpu'
                ]
            )
            page: Page = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={'width': 1280, 'height': 720}
            )

            async def handle_response(response):
                nonlocal crisp_found_in_network
                try:
                    if "crisp.chat" in response.url:
                        logger.info(f"  > Crisp found in network URL: {response.url} for {url}")
                        crisp_found_in_network = True
                        return

                    if 200 <= response.status <= 299:
                        try:
                            response_content = await response.text()
                            if "crisp.chat" in response_content or "CRISP_WEBSITE_ID" in response_content:
                                logger.info(f"  > Crisp found in network content from: {response.url} for {url}")
                                crisp_found_in_network = True
                                return
                        except Exception:
                            pass
                except Exception as e:
                    logger.error(f"  > Error during response handling for {response.url} ({url}): {e}")

            page.on("response", handle_response)
            logger.info(f"  > Response event listener attached for {url}.")

            # --- MODIFIED: More aggressive resource blocking ---
            await page.route("**/*", lambda route: (
                route.abort()
                if route.request.resource_type in ["image", "stylesheet", "font", "media", "manifest", "other"]
                else route.continue_()
            ))
            logger.info(f"  > More aggressive resource blocking enabled for {url}.")

            try:
                logger.info(f"  > Navigating to {url}")
                # Use domcontentloaded for faster initial load
                await page.goto(url, timeout=30000, wait_until='domcontentloaded')
                logger.info(f"  > DOMContentLoaded reached for {url}.")

                if crisp_found_in_network:
                    logger.info(f"  > Crisp found via network interception (early detection) for {url}.")
                    return "Uses Crisp", ""

                # Attempt to click chat button (if present)
                try:
                    logger.info(f"  > Trying to click chat button for {url}")
                    chat_button_locator = page.locator("div[class*='chat'], button[class*='chat'], [data-crisp-id], #crisp-chatbox")
                    if await chat_button_locator.count() > 0:
                        await chat_button_locator.first.click(timeout=5000)
                        await page.wait_for_timeout(3000) # Give some time for Crisp to initialize after click
                    else:
                        logger.info(f"  > No obvious chat button found for {url}.")
                except TimeoutError:
                    logger.warning(f"  > Timeout while clicking chat button for {url}. Continuing...")
                    pass
                except Exception as e:
                    logger.warning(f"  > Error clicking chat button for {url}: {e}. Continuing...")
                    pass

                if crisp_found_in_network:
                    logger.info(f"  > Crisp found via network interception (after click) for {url}.")
                    return "Uses Crisp", ""

                # --- Targeted JavaScript check for Crisp objects ---
                logger.info(f"  > Waiting for Crisp JS objects for {url}...")
                try:
                    await page.wait_for_function(
                        "() => typeof $crisp !== 'undefined' || typeof CRISP_WEBSITE_ID !== 'undefined'",
                        timeout=10000 # Shorter timeout for JS object presence
                    )
                    logger.info(f"  > Crisp found via JavaScript evaluation on {url}.")
                    return "Uses Crisp", ""
                except TimeoutError:
                    logger.info(f"  > Crisp JS objects not found within timeout for {url}. Proceeding to networkidle/content check.")
                    pass # Continue if JS objects are not immediately present

                # Fallback to networkidle and content check if JS objects not found quickly
                logger.info(f"  > Waiting for network to be idle (fallback) for {url}...")
                await page.wait_for_load_state('networkidle', timeout=15000) # Reduced timeout for networkidle
                logger.info(f"  > Network idle state reached (or timeout) for {url}.")

                crisp_in_js_fallback = await page.evaluate("""
                    () => {
                        return typeof $crisp !== 'undefined' || typeof CRISP_WEBSITE_ID !== 'undefined';
                    }
                """)
                if crisp_in_js_fallback:
                    logger.info(f"  > Crisp found via JavaScript evaluation (fallback) on {url}.")
                    return "Uses Crisp", ""

                content = await page.content()
                logger.info(f"  > Checking HTML content for Crisp indicators for {url}...")
                if "crisp.chat" in content or "CRISP_WEBSITE_ID" in content:
                    logger.info(f"  > Crisp found in HTML content for {url}.")
                    return "Uses Crisp", ""

                if crisp_found_in_network:
                    logger.info(f"  > Crisp found via network interception (final check) for {url}.")
                    return "Uses Crisp", ""

                logger.info(f"  > Crisp NOT found after all checks for {url}.")
                return "Does NOT use Crisp", ""

            except TimeoutError as e:
                error_msg = f"Timeout: {e}"
                if crisp_found_in_network:
                    logger.warning(f"  > Crisp was detected despite a TimeoutError for {url}. Reporting as 'Uses Crisp'. Error: {error_msg}")
                    return "Uses Crisp", error_msg
                else:
                    logger.error(f"  > Error checking {url}: {error_msg}")
                    return "Error", error_msg
            except Exception as e:
                error_msg = str(e)
                if "net::ERR_NAME_NOT_RESOLVED" in error_msg:
                    error_msg = "Network Error: DNS Resolution Failed"
                elif "net::ERR_CONNECTION_REFUSED" in error_msg:
                    error_msg = "Network Error: Connection Refused"
                elif "Target page, context or browser has been closed" in error_msg:
                    error_msg = "Playwright Error: Browser/Page Closed Unexpectedly"
                elif "net::ERR_CERT_COMMON_NAME_INVALID" in error_msg:
                    error_msg = "SSL Certificate Error: Common Name Invalid"
                elif "net::ERR_ABORTED" in error_msg:
                    error_msg = "Network Error: Request Aborted (often due to navigation or page close)"
                elif "browserType.launch: Executable doesn't exist" in error_msg:
                    error_msg = "Playwright Error: Browser executable not found (run 'playwright install')"

                if crisp_found_in_network:
                    logger.warning(f"  > Crisp was detected despite an error for {url}. Reporting as 'Uses Crisp'. Error: {error_msg}")
                    return "Uses Crisp", error_msg
                else:
                    logger.error(f"  > Error checking {url}: {error_msg}")
                    return "Error", error_msg

            finally:
                await browser.close() # Close the browser after each check


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
            if row.get('url'):
                websites.append(row['url'].strip())
        logger.info(f"  > Found {len(websites)} websites.")
        return websites


def write_csv(results, output_file):
    fieldnames = ['url', 'status', 'error']
    with open(output_file, mode='w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(row)
    logger.info(f"Results saved to {output_file}")


def write_html(results, output_file):
    uses_crisp_count = sum(1 for r in results if r['status'] == 'Uses Crisp')
    not_uses_crisp_count = sum(1 for r in results if r['status'] == 'Does NOT use Crisp')
    error_count = sum(1 for r in results if r['status'] == 'Error')
    total_count = len(results)

    # Corrected HTML content with escaped curly braces for f-string
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
            --bg-primary: #fafbfc;
            --bg-secondary: #ffffff;
            --bg-tertiary: #f8fafc;
            --text-primary: #1a202c;
            --text-secondary: #4a5568;
            --text-muted: #718096;
            --border-light: #e2e8f0;
            --border-medium: #cbd5e0;
            --accent-blue: #3182ce;
            --accent-blue-light: #bee3f8;
            --success: #38a169;
            --success-light: #c6f6d5;
            --error: #e53e3e;
            --error-light: #fed7d7;
            --warning: #d69e2e;
            --warning-light: #faf089;
            --shadow-sm: 0 1px 3px 0 rgba(0, 0, 0, 0.1), 0 1px 2px 0 rgba(0, 0, 0, 0.06);
            --shadow-md: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
            --shadow-lg: 0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05);
            --radius-sm: 6px;
            --radius-md: 8px;
            --radius-lg: 12px;
            --radius-xl: 16px;
        }}

        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
            font-size: 14px;
            min-height: 100vh;
            padding: 24px;
        }}

        .container {{
            max-width: 1200px;
            margin: 0 auto;
            display: flex;
            flex-direction: column;
            gap: 24px;
        }}

        .header {{
            text-align: center;
            margin-bottom: 8px;
        }}

        .header h1 {{
            font-size: 32px;
            font-weight: 700;
            color: var(--text-primary);
            margin-bottom: 8px;
            letter-spacing: -0.5px;
        }}

        .header .subtitle {{
            color: var(--text-muted);
            font-size: 16px;
            font-weight: 400;
        }}

        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
            margin-bottom: 8px;
        }}

        .stat-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-light);
            border-radius: var(--radius-lg);
            padding: 24px;
            text-align: center;
            box-shadow: var(--shadow-sm);
            transition: all 0.2s ease;
            position: relative;
            overflow: hidden;
        }}

        .stat-card:hover {{
            transform: translateY(-2px);
            box-shadow: var(--shadow-md);
        }}

        .stat-card::before {{
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            border-radius: var(--radius-lg) var(--radius-lg) 0 0;
        }}

        .stat-card.total::before {{ background: linear-gradient(90deg, var(--accent-blue), #4299e1); }}
        .stat-card.success::before {{ background: linear-gradient(90deg, var(--success), #48bb78); }}
        .stat-card.error::before {{ background: linear-gradient(90deg, var(--error), #f56565); }}
        .stat-card.warning::before {{ background: linear-gradient(90deg, var(--warning), #ecc94b); }}

        .stat-number {{
            font-size: 36px;
            font-weight: 700;
            margin-bottom: 8px;
            line-height: 1;
        }}

        .stat-card.total .stat-number {{ color: var(--accent-blue); }}
        .stat-card.success .stat-number {{ color: var(--success); }}
        .stat-card.error .stat-number {{ color: var(--error); }}
        .stat-card.warning .stat-number {{ color: var(--warning); }}

        .stat-label {{
            font-size: 14px;
            color: var(--text-secondary);
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .controls {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-light);
            border-radius: var(--radius-lg);
            padding: 20px 24px;
            box-shadow: var(--shadow-sm);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }}

        .search-box {{
            display: flex;
            align-items: center;
            gap: 12px;
            flex: 1;
            max-width: 400px;
        }}

        .search-input {{
            flex: 1;
            padding: 10px 16px;
            border: 1px solid var(--border-medium);
            border-radius: var(--radius-md);
            font-size: 14px;
            background: var(--bg-tertiary);
            transition: all 0.2s ease;
            outline: none;
        }}

        .search-input:focus {{
            border-color: var(--accent-blue);
            background: var(--bg-secondary);
            box-shadow: 0 0 0 3px var(--accent-blue-light);
        }}

        .filter-select {{
            padding: 10px 16px;
            border: 1px solid var(--border-medium);
            border-radius: var(--radius-md);
            background: var(--bg-secondary);
            font-size: 14px;
            color: var(--text-primary);
            cursor: pointer;
            outline: none;
            transition: all 0.2s ease;
            min-width: 160px;
        }}

        .filter-select:focus {{
            border-color: var(--accent-blue);
            box-shadow: 0 0 0 3px var(--accent-blue-light);
        }}

        .table-container {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-light);
            border-radius: var(--radius-lg);
            overflow: hidden;
            box-shadow: var(--shadow-sm);
        }}

        .table-wrapper {{
            overflow-x: auto;
        }}

        table {{
            width: 100%;
            border-collapse: collapse;
        }}

        thead {{
            background: var(--bg-tertiary);
            position: sticky;
            top: 0;
            z-index: 10;
        }}

        th {{
            padding: 16px 20px;
            text-align: left;
            font-weight: 600;
            font-size: 12px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 2px solid var(--border-light);
        }}

        tbody tr {{
            border-bottom: 1px solid var(--border-light);
            transition: background-color 0.15s ease;
        }}

        tbody tr:hover {{
            background: var(--bg-tertiary);
        }}

        tbody tr:last-child {{
            border-bottom: none;
        }}

        td {{
            padding: 16px 20px;
            vertical-align: middle;
        }}

        .url-cell {{
            max-width: 300px;
        }}

        .url-link {{
            color: var(--accent-blue);
            text-decoration: none;
            font-weight: 500;
            display: block;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            transition: color 0.2s ease;
        }}

        .url-link:hover {{
            color: #2c5aa0;
            text-decoration: underline;
        }}

        .status-badge {{
            display: inline-flex;
            align-items: center;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.3px;
        }}

        .status-badge.uses {{
            background: var(--success-light);
            color: var(--success);
        }}

        .status-badge.not-uses {{
            background: var(--error-light);
            color: var(--error);
        }}

        .status-badge.error {{
            background: var(--warning-light);
            color: var(--warning);
        }}

        .badge-icon {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }}

        .badge.uses .badge-icon {{ background: var(--success); }}
        .badge.not-uses .badge-icon {{ background: var(--error); }}
        .badge.error .badge-icon {{ background: var(--warning); }}

        .error-cell {{
            color: var(--text-muted);
            font-size: 13px;
            max-width: 200px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        .hidden {{
            display: none !important;
        }}

        .empty-state {{
            text-align: center;
            padding: 48px 20px;
            color: var(--text-muted);
        }}

        .empty-state-icon {{
            font-size: 48px;
            margin-bottom: 16px;
            opacity: 0.5;
        }}

        @media (max-width: 768px) {{
            body {{ padding: 16px; }}
            .container {{ gap: 16px; }}
            .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
            .controls {{
                flex-direction: column;
                align-items: stretch;
            }}
            .search-box {{ max-width: none; }}
            th, td {{ padding: 12px 16px; }}
            .header h1 {{ font-size: 24px; }}
        }}

        @media (max-width: 480px) {{
            .stats-grid {{ grid-template-columns: 1fr; }}
            .stat-number {{ font-size: 28px; }}
        }}
    </style>
</head>

<body>
    <div class="container">
        <div class="header">
            <h1>Crisp Integration Dashboard</h1>
            <p class="subtitle">Website analysis results and integration status</p>
        </div>

        <div class="stats-grid">
            <div class="stat-card total">
                <div class="stat-number">{total_count}</div>
                <div class="stat-label">Total Checked</div>
            </div>
            <div class="stat-card success">
                <div class="stat-number">{uses_crisp_count}</div>
                <div class="stat-label">Uses Crisp</div>
            </div>
            <div class="stat-card error">
                <div class="stat-number">{not_uses_crisp_count}</div>
                <div class="stat-label">No Crisp Found</div>
            </div>
            <div class="stat-card warning">
                <div class="stat-number">{error_count}</div>
                <div class="stat-label">Check Errors</div>
            </div>
        </div>

        <div class="controls">
            <div class="search-box">
                <input type="text" id="searchInput" class="search-input" placeholder="Search websites...">
            </div>
            <select id="statusFilter" class="filter-select" onchange="filterTable()">
                <option value="all">All Results</option>
                <option value="uses">Uses Crisp</option>
                <option value="not-uses">No Crisp</option>
                <option value="error">Errors</option>
            </select>
        </div>

        <div class="table-container">
            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Website</th>
                            <th>Status</th>
                            <th>Error Details</th>
                        </tr>
                    </thead>
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
                            <td class="url-cell">
                                <a href="{row['url']}" target="_blank" class="url-link" title="{row['url']}">{row['url']}</a>
                            </td>
                            <td>
                                <span class="status-badge {status_class}">{row['status']}</span>
                            </td>
                            <td class="error-cell" title="{row['error']}">{row['error']}</td>
                        </tr>
"""

    html_content += """
                    </tbody>
                </table>
                <div id="emptyState" class="empty-state hidden">
                    <p>No results found matching your search criteria.</p>
                </div>
            </div>
        </div>
    </div>

    <script>
        const searchInput = document.getElementById('searchInput');
        const statusFilter = document.getElementById('statusFilter');
        const resultsTable = document.getElementById('resultsTable');
        const emptyState = document.getElementById('emptyState');

        function filterTable() {
            const searchTerm = searchInput.value.toLowerCase();
            const filterValue = statusFilter.value;
            const rows = resultsTable.getElementsByTagName('tr');
            let visibleCount = 0;

            for (let i = 0; i < rows.length; i++) {
                const row = rows[i];
                const url = row.getAttribute('data-url') || '';
                const matchesSearch = url.includes(searchTerm);
                const matchesFilter = filterValue === 'all' || row.classList.contains(filterValue);

                if (matchesSearch && matchesFilter) {
                    row.classList.remove('hidden');
                    visibleCount++;
                } else {
                    row.classList.add('hidden');
                }
            }

            // Show/hide empty state
            if (visibleCount === 0) {
                emptyState.classList.remove('hidden');
            } else {
                emptyState.classList.add('hidden');
            }
        }

        // Add event listeners
        searchInput.addEventListener('input', filterTable);
        statusFilter.addEventListener('change', filterTable);

        // Initialize
        filterTable();
    </script>
</body>

</html>
"""

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(html_content)
    logger.info(f"HTML dashboard saved to {output_file}")


async def main():
    parser = argparse.ArgumentParser(description="Crisp Chat Detector for Websites")
    parser.add_argument(
        "-i", "--input",
        type=str,
        default=DEFAULT_INPUT_FILE,
        help=f"Path to the input CSV file containing website URLs (default: {DEFAULT_INPUT_FILE})"
    )
    parser.add_argument(
        "-c", "--csv-output",
        type=str,
        default=DEFAULT_CSV_OUTPUT_FILE,
        help=f"Name of the CSV output file (default: {DEFAULT_CSV_OUTPUT_FILE})"
    )
    parser.add_argument(
        "-H", "--html-output",
        type=str,
        default=DEFAULT_HTML_OUTPUT_FILE,
        help=f"Name of the HTML dashboard file (default: {DEFAULT_HTML_OUTPUT_FILE})"
    )
    parser.add_argument(
        "-j", "--jobs",
        type=int,
        default=DEFAULT_CONCURRENT_CHECKS,
        help=f"Number of concurrent website checks (default: {DEFAULT_CONCURRENT_CHECKS})"
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run Playwright in headful mode (show browser UI) for debugging."
    )
    parser.add_argument(
        "-l", "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)"
    )

    args = parser.parse_args()

    logger.setLevel(args.log_level.upper())

    logger.info("Starting CrispChecker script...")
    websites = read_websites(args.input)
    results = []

    if not websites:
        logger.warning("No websites found in the input file. Please check your input file or path.")
        return

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(
            headless=not args.headful,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--no-first-run',
                '--no-zygote',
                '--single-process',
                '--disable-gpu'
            ]
        )
        semaphore = asyncio.Semaphore(args.jobs)
        tasks = [check_crisp(url, semaphore, not args.headful) for url in websites]
        checked_results = await asyncio.gather(*tasks)
        await browser.close()

    for i, url in enumerate(websites):
        status, error_message = checked_results[i]
        results.append({
            'url': url,
            'status': status,
            'error': error_message
        })
        logger.info(f"{url} -> {status} (Error: {error_message})" if error_message else f"{url} -> {status}")

    write_csv(results, args.csv_output)
    write_html(results, args.html_output)

    logger.info(f"\n✅ Results saved to {args.csv_output} and {args.html_output}")
    logger.info("CrispChecker script finished.")


if __name__ == '__main__':
    asyncio.run(main())
