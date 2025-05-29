from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List
import asyncio
sys.path.append(os.getcwd())  # Add the current working directory to Python's path
from CrispCheckerV3 import check_crisp

app = FastAPI()

# Serve os arquivos estáticos
app.mount("/static", StaticFiles(directory="static"), name="static")

class URLList(BaseModel):
    urls: List[str]

@app.get("/", response_class=HTMLResponse)
async def serve_frontend():
    return FileResponse("static/index.html")


@app.post("/check")
async def check_urls(data: URLList):
    semaphore = asyncio.Semaphore(5)
    # In this version, check_crisp does NOT receive a 'browser' argument
    tasks = [check_crisp(url, semaphore, headless_mode=True) for url in data.urls]
    results = await asyncio.gather(*tasks)

    output = []
    for url, (status, error) in zip(data.urls, results):
        output.append({
            "url": url,
            "status": status,
            "error": error
        })

    return JSONResponse(content={"results": output})

