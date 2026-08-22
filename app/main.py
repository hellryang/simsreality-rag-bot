from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pathlib import Path

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"

@app.get("/", response_class=HTMLResponse)
def get_index_page():
    file_path = TEMPLATES_DIR / "index.html"
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()