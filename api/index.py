import sys
import os
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse

app = FastAPI()

@app.get("/")
def health():
    return {"status": "ok"}

@app.post("/score")
async def score_resume(resume: UploadFile = File(...), role: str = Form("software_engineering_intern")):
    from evaluator import Evaluator  # adjust import to match actual score.py internals
    from pdf import PDFHandler

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(await resume.read())
        tmp_path = tmp.name

    try:
        # you'll need to refactor score.py's __main__ logic into a callable
        # function, since it's currently written for CLI args + stdout only
        result = run_scoring_pipeline(tmp_path, role)
        return JSONResponse(result)
    finally:
        os.unlink(tmp_path)
