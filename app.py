# app.py

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict, Any
import asyncio
import logging

from CrispCheckerV3 import check_crisp  # check_crisp will need to accept new timeout args
from playwright.async_api import async_playwright, Playwright, Browser

logger = logging.getLogger("uvicorn.error")
logger.setLevel(logging.INFO)

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")

playwright_context: Playwright = None
browser_instance: Browser = None
SHARED_BROWSER_HEADLESS_MODE = True
# Default values, will be overridden by client if provided
DEFAULT_CONCURRENCY_WS = 5


@app.on_event("startup")
async def startup_event():
    global playwright_context, browser_instance
    try:
        logger.info("FastAPI startup: Initializing Playwright...")
        playwright_context = await async_playwright().start()
        logger.info("FastAPI startup: Launching shared browser instance...")
        browser_instance = await playwright_context.chromium.launch(
            headless=SHARED_BROWSER_HEADLESS_MODE,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas', '--no-first-run', '--no-zygote',
                '--disable-gpu'
            ]
        )
        logger.info(f"FastAPI startup: Playwright browser launched (headless: {SHARED_BROWSER_HEADLESS_MODE}).")
    except Exception as e:
        logger.error(f"FastAPI startup: Error initializing Playwright or launching browser: {e}", exc_info=True)
        playwright_context = None
        browser_instance = None


@app.on_event("shutdown")
async def shutdown_event():
    global playwright_context, browser_instance
    logger.info("FastAPI shutdown: Cleaning up Playwright resources...")
    if browser_instance and browser_instance.is_connected():
        try:
            await browser_instance.close()
            logger.info("FastAPI shutdown: Playwright browser closed.")
        except Exception as e:
            logger.error(f"FastAPI shutdown: Error closing browser: {e}", exc_info=True)
    if playwright_context:
        try:
            await playwright_context.stop()
            logger.info("FastAPI shutdown: Playwright context stopped.")
        except Exception as e:
            logger.error(f"FastAPI shutdown: Error stopping Playwright context: {e}", exc_info=True)


class URLList(BaseModel):
    urls: List[str]


@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return FileResponse("static/index.html")


@app.post("/check")  # HTTP endpoint, does not use user-configurable options for now
async def check_urls_endpoint(data: URLList):
    if not browser_instance or not browser_instance.is_connected():
        logger.error("HTTP /check: Browser not initialized or disconnected.")
        return JSONResponse(
            content={"error": "Browser service is temporarily unavailable. Please try again later."},
            status_code=503
        )

    # HTTP endpoint uses a fixed concurrency for now
    limiter = asyncio.Semaphore(MAX_CONCURRENT_CHECKS_HTTP)
    tasks = []

    async def process_single_url_with_limiter(url_to_check: str):
        async with limiter:
            logger.info(f"HTTP /check: Processing URL: {url_to_check}")
            # Using default timeouts for HTTP endpoint
            status, error, _ = await check_crisp(
                url_to_check,
                browser_instance
                # Default timeouts will be used by check_crisp if not provided
            )
            return status, error

    for url in data.urls:
        if url and url.strip():
            tasks.append(process_single_url_with_limiter(url.strip()))

    results_from_gather = await asyncio.gather(*tasks, return_exceptions=True)

    output = []
    valid_urls_processed = [url.strip() for url in data.urls if url and url.strip()]

    for i, url_processed in enumerate(valid_urls_processed):
        result_item = results_from_gather[i]
        if isinstance(result_item, Exception):
            logger.error(f"HTTP /check: Error processing URL {url_processed}: {result_item}")
            output.append(
                {"url": url_processed, "status": "Error", "error": f"Task execution failed: {str(result_item)}"})
        else:
            status, error = result_item
            output.append({"url": url_processed, "status": status, "error": error})

    return JSONResponse(content={"results": output})


@app.websocket("/check_ws")
async def websocket_check(websocket: WebSocket):
    await websocket.accept()

    active_processing_tasks: List[asyncio.Task] = []
    cancellation_requested = False

    try:
        initial_data = await websocket.receive_json()
        if initial_data.get("type") != "url_submission":
            logger.warning(f"WS /check_ws: Unexpected initial message type: {initial_data.get('type')}")
            await websocket.send_json(
                {"type": "error_message", "error": "Invalid initial message. Expected URL submission.",
                 "complete": True, "results": []})
            await websocket.close(code=1003)
            return

        urls_to_check = [u.strip() for u in initial_data.get("urls", []) if u and u.strip()]

        # Extract options from the initial message
        options = initial_data.get("options", {})
        concurrency = options.get("concurrency", DEFAULT_CONCURRENCY_WS)
        page_load_timeout = options.get("pageLoadTimeout", 30000)  # Default 30s
        js_check_timeout = options.get("jsCheckTimeout", 10000)  # Default 10s
        network_idle_timeout = options.get("networkIdleTimeout", 15000)  # Default 15s

        logger.info(
            f"WS /check_ws: Received options - Concurrency: {concurrency}, PageLoadTO: {page_load_timeout}, JSCheckTO: {js_check_timeout}, NetworkIdleTO: {network_idle_timeout}")

        total_urls = len(urls_to_check)
        if total_urls == 0:
            await websocket.send_json({"type": "completion", "complete": True, "results": [], "progress": 100})
            await websocket.close()
            return

        if not browser_instance or not browser_instance.is_connected():
            logger.error("WS /check_ws: Browser not initialized or disconnected during URL processing setup.")
            await websocket.send_json(
                {"type": "error_message", "error": "Browser service unavailable.", "complete": True, "results": []})
            await websocket.close(code=1011)
            return

        completed_count = 0
        # Use the concurrency value from options for the semaphore
        limiter_ws = asyncio.Semaphore(concurrency)
        all_results_summary = []

        async def process_url_task_ws(current_url: str):
            nonlocal completed_count, cancellation_requested
            status_ws, error_msg_ws = "Error", "Task execution error"
            log_entries_ws = []

            if cancellation_requested:
                logger.info(f"WS /check_ws: Check for {current_url} skipped due to cancellation request.")
                if websocket.client_state == websocket.client_state.CONNECTED:
                    await websocket.send_json({"type": "log_entry", "url": current_url,
                                               "message": f"INFO: Check for {current_url} skipped (cancellation)."})
                all_results_summary.append(
                    {"url": current_url, "status": "Cancelled", "error": "Operation cancelled by user."})
                completed_count += 1
                return

            try:
                async with limiter_ws:
                    if cancellation_requested:
                        logger.info(
                            f"WS /check_ws: Check for {current_url} (post-semaphore) skipped due to cancellation.")
                        if websocket.client_state == websocket.client_state.CONNECTED:
                            await websocket.send_json({"type": "log_entry", "url": current_url,
                                                       "message": f"INFO: Check for {current_url} skipped (cancellation)."})
                        all_results_summary.append(
                            {"url": current_url, "status": "Cancelled", "error": "Operation cancelled by user."})
                        completed_count += 1
                        return

                    logger.info(
                        f"WS /check_ws: Processing URL: {current_url} with PageTO: {page_load_timeout}, JSTO: {js_check_timeout}, IdleTO: {network_idle_timeout}")
                    # Pass the received timeout options to check_crisp
                    status_ws, error_msg_ws, log_entries_ws = await check_crisp(
                        current_url,
                        browser_instance,
                        page_load_timeout_ms=page_load_timeout,
                        js_check_timeout_ms=js_check_timeout,
                        network_idle_timeout_ms=network_idle_timeout
                    )

                for entry in log_entries_ws:
                    if websocket.client_state == websocket.client_state.CONNECTED:
                        await websocket.send_json({"type": "log_entry", "url": current_url, "message": entry})
                    else:
                        raise WebSocketDisconnect(code=1001, reason="Client disconnected during log sending")

            except asyncio.CancelledError:
                logger.info(f"WS /check_ws: Task for {current_url} was cancelled.")
                status_ws, error_msg_ws = "Cancelled", "Operation cancelled by user."
                if websocket.client_state == websocket.client_state.CONNECTED:
                    await websocket.send_json({"type": "log_entry", "url": current_url,
                                               "message": f"INFO: Check for {current_url} cancelled."})
            except WebSocketDisconnect as e:
                logger.warning(
                    f"WS /check_ws: Client disconnected during processing of {current_url}. Reason: {e.reason}")
                status_ws, error_msg_ws = "Error", "Client disconnected during processing."
            except Exception as e_task:
                logger.error(f"WS /check_ws: Error in task for URL {current_url}: {e_task}", exc_info=True)
                error_msg_ws = f"Failed to process URL: {str(e_task)}"
                if websocket.client_state == websocket.client_state.CONNECTED:
                    await websocket.send_json({"type": "log_entry", "url": current_url,
                                               "message": f"[ERROR] Task failed for {current_url}: {error_msg_ws}"})
            finally:
                completed_count += 1
                progress_percentage = int((completed_count / total_urls) * 100) if total_urls > 0 else 100
                logger.info(
                    f"WS /check_ws: Preparing to send progress_update for {current_url}. Progress: {progress_percentage}%, Completed: {completed_count}/{total_urls}")

                if websocket.client_state == websocket.client_state.CONNECTED:
                    try:
                        await websocket.send_json({
                            "type": "progress_update",
                            "progress": progress_percentage,
                            "current_url_processed": current_url,
                            "status": status_ws
                        })
                        logger.info(f"WS /check_ws: Sent progress_update for {current_url}.")
                    except WebSocketDisconnect:
                        logger.warning(f"WS /check_ws: Client disconnected during progress update for {current_url}.")
                    except Exception as e_send:
                        logger.error(f"WS /check_ws: Error sending progress for {current_url}: {e_send}")

                all_results_summary.append({"url": current_url, "status": status_ws, "error": error_msg_ws})

        for url_item in urls_to_check:
            task = asyncio.create_task(process_url_task_ws(url_item))
            active_processing_tasks.append(task)

        async def message_receiver():
            nonlocal cancellation_requested
            while websocket.client_state == websocket.client_state.CONNECTED and not cancellation_requested:
                try:
                    message = await websocket.receive_json()
                    if message.get("type") == "cancel_checks":
                        logger.info("WS /check_ws: Cancellation request received from client.")
                        cancellation_requested = True
                        for t in active_processing_tasks:
                            if not t.done():
                                t.cancel()
                        if websocket.client_state == websocket.client_state.CONNECTED:
                            await websocket.send_json({"type": "cancelled"})
                        break
                except WebSocketDisconnect:
                    logger.info("WS /check_ws: Client disconnected while listening for messages.")
                    cancellation_requested = True
                    for t in active_processing_tasks:
                        if not t.done(): t.cancel()
                    break
                except Exception as e_recv:
                    logger.error(f"WS /check_ws: Error receiving/processing client message: {e_recv}")
                    if isinstance(e_recv, (ValueError, TypeError)):
                        logger.warning("WS /check_ws: Invalid message format from client.")
                    else:
                        cancellation_requested = True
                        for t in active_processing_tasks:
                            if not t.done(): t.cancel()
                        break

        receiver_task = asyncio.create_task(message_receiver())

        await asyncio.gather(*active_processing_tasks, return_exceptions=True)

        if not receiver_task.done():
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                logger.debug("WS /check_ws: Receiver task cancelled as expected after processing loop.")

        if websocket.client_state == websocket.client_state.CONNECTED:
            if cancellation_requested:
                logger.info("WS /check_ws: Process was cancelled. Sending final results.")
            else:
                logger.info("WS /check_ws: All tasks completed normally. Sending final results.")

            final_progress = 100
            await websocket.send_json({
                "type": "completion",
                "complete": True,
                "results": all_results_summary,
                "progress": final_progress
            })

        if websocket.client_state == websocket.client_state.CONNECTED:
            await websocket.close()

    except WebSocketDisconnect:
        logger.info("WS /check_ws: WebSocket disconnected (outer handler).")
        cancellation_requested = True
        for task in active_processing_tasks:
            if not task.done(): task.cancel()
    except Exception as e:
        logger.error(f"WS /check_ws: Unhandled error in websocket_check: {e}", exc_info=True)
        cancellation_requested = True
        for task in active_processing_tasks:
            if not task.done(): task.cancel()
        try:
            if websocket.client_state == websocket.client_state.CONNECTED:
                await websocket.send_json(
                    {"type": "error_message", "error": f"A critical server error occurred: {str(e)}", "complete": True,
                     "results": []})
                await websocket.close(code=1011)
        except Exception as e_ws_close:
            logger.error(f"WS /check_ws: Error sending critical error message or closing WebSocket: {e_ws_close}")
    finally:
        for task_to_clean in active_processing_tasks:
            if not task_to_clean.done():
                task_to_clean.cancel()
                try:
                    await task_to_clean
                except asyncio.CancelledError:
                    pass
                except Exception as e_final_clean:
                    logger.error(f"Error during final task cleanup: {e_final_clean}")
        logger.info("WS /check_ws: WebSocket handler finished.")

