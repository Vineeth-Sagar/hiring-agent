"""Vercel serverless entrypoint: a small web UI + JSON API around score.py.

GET  /        -> HTML page with a file picker + role selector
GET  /health  -> {"status": "ok"}
POST /score   -> multipart form (resume=<pdf>, role=<name>) -> evaluation JSON
"""

import os
import sys
import tempfile

# This file lives in api/; make the repo root importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Vercel's filesystem is read-only apart from /tmp. Force the library code off
# its cache/CSV-writing paths and off the slow GitHub-enrichment step before any
# project module is imported. Dashboard env vars still win via setdefault.
os.environ.setdefault("DEVELOPMENT_MODE", "false")
os.environ.setdefault("ENABLE_GITHUB_ENRICHMENT", "false")

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from roles import list_available_roles, load_role
from score import main as run_scoring

app = FastAPI(title="hiring-agent")

DEFAULT_ROLE = "software_engineering_intern"


def _summarise(evaluation, role) -> dict:
    """Reproduce score.print_evaluation_results' score math as plain data."""
    total = 0.0
    max_score = 0
    categories = []

    if getattr(evaluation, "scores", None):
        for category in role.categories:
            cat = getattr(evaluation.scores, category.key, None)
            if not cat:
                continue
            capped = min(cat.score, category.max)
            total += capped
            max_score += category.max
            categories.append(
                {
                    "key": category.key,
                    "label": category.label,
                    "score": capped,
                    "max": category.max,
                    "evidence": getattr(cat, "evidence", None),
                }
            )

    bonus = getattr(getattr(evaluation, "bonus_points", None), "total", 0) or 0
    deductions = getattr(getattr(evaluation, "deductions", None), "total", 0) or 0
    total += bonus - deductions

    max_possible = max_score + role.bonus_max
    total = max(0.0, min(total, max_possible))

    return {
        "overall_score": round(total, 1),
        "max_score": max_score,
        "max_possible_score": max_possible,
        "bonus_points": bonus,
        "deductions": deductions,
        "categories": categories,
        "key_strengths": list(getattr(evaluation, "key_strengths", []) or []),
        "areas_for_improvement": list(
            getattr(evaluation, "areas_for_improvement", []) or []
        ),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index():
    roles = list_available_roles() or [DEFAULT_ROLE]
    options = "\n".join(
        f'<option value="{r}"{" selected" if r == DEFAULT_ROLE else ""}>{r}</option>'
        for r in roles
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hiring-agent</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 720px; margin: 3rem auto; padding: 0 1rem; }}
  h1 {{ margin-bottom: .25rem; }}
  form {{ display: grid; gap: 1rem; margin: 1.5rem 0; }}
  label {{ font-weight: 600; }}
  button {{ padding: .6rem 1rem; font-size: 1rem; cursor: pointer; width: fit-content; }}
  button[disabled] {{ opacity: .5; cursor: progress; }}
  pre {{ background: rgba(127,127,127,.12); padding: 1rem; border-radius: 8px; overflow: auto; white-space: pre-wrap; }}
  .score {{ font-size: 2rem; font-weight: 700; }}
  .muted {{ opacity: .7; font-size: .9rem; }}
</style>
</head>
<body>
<h1>hiring-agent</h1>
<p class="muted">Upload a resume PDF and score it against a role rubric.</p>
<form id="f">
  <div>
    <label for="role">Role</label><br>
    <select id="role" name="role">{options}</select>
  </div>
  <div>
    <label for="resume">Resume PDF</label><br>
    <input id="resume" name="resume" type="file" accept="application/pdf" required>
  </div>
  <button type="submit">Score resume</button>
</form>
<div id="out"></div>
<script>
const f = document.getElementById('f');
const out = document.getElementById('out');
f.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const btn = f.querySelector('button');
  btn.disabled = true;
  out.innerHTML = '<p class="muted">Scoring… this can take up to a minute.</p>';
  try {{
    const res = await fetch('/score', {{ method: 'POST', body: new FormData(f) }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || JSON.stringify(data));
    const s = data.summary;
    out.innerHTML =
      '<p class="score">' + s.overall_score + ' / ' + s.max_score + '</p>' +
      '<pre>' + JSON.stringify(data, null, 2) + '</pre>';
  }} catch (err) {{
    out.innerHTML = '<p style="color:#c00">' + err.message + '</p>';
  }} finally {{
    btn.disabled = false;
  }}
}});
</script>
</body>
</html>"""


@app.post("/score")
async def score_endpoint(
    resume: UploadFile = File(...),
    role: str = Form(DEFAULT_ROLE),
):
    if role not in list_available_roles():
        raise HTTPException(400, f"Unknown role '{role}'.")
    try:
        role_obj = load_role(role)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(await resume.read())

        evaluation = run_scoring(tmp_path, role_obj)
        if evaluation is None:
            raise HTTPException(422, "Could not extract resume data from the PDF.")

        return JSONResponse(
            {
                "role": role,
                "summary": _summarise(evaluation, role_obj),
                "raw": evaluation.model_dump(),
            }
        )
    except HTTPException:
        raise
    except Exception as exc:  # surface the failure instead of a bare 500
        raise HTTPException(500, f"{type(exc).__name__}: {exc}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
