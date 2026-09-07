"""Vercel entrypoint: a polished web UI + JSON API around score.py.

Placed at the project root as ``app.py`` with a top-level ``app`` so Vercel's
zero-config Python/FastAPI detection routes every request here (no vercel.json
rewrites needed).

    GET  /        -> HTML single-page app (upload a resume, see the score)
    GET  /health  -> {"status": "ok"}
    GET  /roles   -> {"roles": [...], "default": "..."}
    POST /score   -> multipart form (resume=<pdf>, role=<name>) -> evaluation JSON
"""

import os
import sys
import tempfile

# Make sibling modules importable no matter what the process CWD is.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Vercel's filesystem is read-only apart from /tmp. Force the library code off
# its cache/CSV-writing paths and off the slow GitHub-enrichment step before any
# project module is imported. Real dashboard env vars still win (setdefault).
os.environ.setdefault("DEVELOPMENT_MODE", "false")
os.environ.setdefault("ENABLE_GITHUB_ENRICHMENT", "false")

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from roles import list_available_roles, load_role
from score import main as run_scoring

app = FastAPI(title="Resume Reality Check")

DEFAULT_ROLE = "software_engineering_intern"


def _roles():
    found = list_available_roles()
    return found or [DEFAULT_ROLE]


def _default_role():
    roles = _roles()
    return DEFAULT_ROLE if DEFAULT_ROLE in roles else roles[0]


def _pretty_role(name: str) -> str:
    return name.replace("_", " ").replace("-", " ").title()


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
                    "icon": getattr(category, "icon", "") or "",
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

    bonus_breakdown = getattr(
        getattr(evaluation, "bonus_points", None), "breakdown", None
    )
    deduction_reasons = getattr(
        getattr(evaluation, "deductions", None), "reasons", None
    )

    return {
        "overall_score": round(total, 1),
        "max_score": max_score,
        "max_possible_score": max_possible,
        "percentage": round(100 * total / max_score, 1) if max_score else 0,
        "bonus_points": bonus,
        "bonus_breakdown": bonus_breakdown,
        "deductions": deductions,
        "deduction_reasons": deduction_reasons,
        "categories": categories,
        "key_strengths": list(getattr(evaluation, "key_strengths", []) or []),
        "areas_for_improvement": list(
            getattr(evaluation, "areas_for_improvement", []) or []
        ),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/roles")
def roles():
    return {
        "roles": [{"value": r, "label": _pretty_role(r)} for r in _roles()],
        "default": _default_role(),
    }


@app.get("/", response_class=HTMLResponse)
def index():
    opts = "".join(
        f'<option value="{r}"{" selected" if r == _default_role() else ""}>'
        f"{_pretty_role(r)}</option>"
        for r in _roles()
    )
    return PAGE.replace("<!--ROLE_OPTIONS-->", opts)


@app.post("/score")
async def score_endpoint(
    resume: UploadFile = File(...),
    role: str = Form(DEFAULT_ROLE),
):
    if role not in _roles():
        raise HTTPException(400, f"Unknown role '{role}'.")
    try:
        role_obj = load_role(role)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    data = await resume.read()
    if not data:
        raise HTTPException(400, "The uploaded file is empty.")
    if not data[:5].startswith(b"%PDF"):
        raise HTTPException(400, "That doesn't look like a PDF file.")
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(413, "PDF is larger than 12 MB.")

    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

        evaluation = run_scoring(tmp_path, role_obj)
        if evaluation is None:
            raise HTTPException(
                422, "Couldn't read enough from that PDF to score it."
            )

        return JSONResponse(
            {
                "role": role,
                "role_label": _pretty_role(role),
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


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Resume Reality Check</title>
<style>
  :root {
    --bg: #f7f7f4;
    --panel: #ffffff;
    --ink: #17190f;
    --muted: #6b6d63;
    --line: #e7e7e0;
    --green: #1f7a54;
    --green-soft: #e9f3ee;
    --amber: #9a6b1f;
    --amber-soft: #f6efe1;
    --red: #b23b3b;
    --shadow: 0 1px 2px rgba(20,20,10,.04), 0 12px 32px rgba(20,20,10,.06);
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; }
  body {
    background: var(--bg);
    color: var(--ink);
    font: 16px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  a { color: var(--green); }
  .wrap { max-width: 760px; margin: 0 auto; padding: 0 20px; }

  header {
    border-bottom: 1px solid var(--line);
    background: color-mix(in srgb, var(--bg) 80%, #fff);
    position: sticky; top: 0; z-index: 5;
    backdrop-filter: saturate(1.4) blur(6px);
  }
  header .wrap {
    display: flex; align-items: center; justify-content: space-between;
    height: 64px;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 650; }
  .brand .mark {
    width: 30px; height: 30px; border-radius: 9px; background: var(--ink);
    color: #fff; display: grid; place-items: center; font-weight: 800; font-size: 15px;
  }
  .ghlink {
    display: inline-flex; align-items: center; gap: 8px;
    border: 1px solid var(--line); border-radius: 999px;
    padding: 7px 14px; font-size: 14px; color: var(--ink); text-decoration: none;
    background: var(--panel);
  }
  .ghlink:hover { border-color: #d8d8cf; }

  main { padding: 56px 0 80px; }
  .eyebrow {
    text-align: center; color: var(--green); font-weight: 600; font-size: 14px;
    letter-spacing: .01em; margin-bottom: 18px;
  }
  h1 {
    text-align: center; font-size: clamp(28px, 5.4vw, 46px); line-height: 1.1;
    letter-spacing: -0.02em; margin: 0 auto 18px; max-width: 17ch; font-weight: 800;
  }
  h1 em { color: var(--green); font-style: normal; }
  .lede {
    text-align: center; color: var(--muted); font-size: 18px;
    max-width: 44ch; margin: 0 auto 34px;
  }

  .card {
    background: var(--panel); border: 1px solid var(--line); border-radius: 20px;
    box-shadow: var(--shadow);
  }

  .controls {
    display: flex; gap: 10px; align-items: center; justify-content: center;
    margin-bottom: 14px; flex-wrap: wrap;
  }
  .controls label { color: var(--muted); font-size: 14px; }
  select {
    font: inherit; font-size: 14px; padding: 8px 12px; border-radius: 10px;
    border: 1px solid var(--line); background: var(--panel); color: var(--ink);
  }

  .drop {
    padding: 48px 24px; text-align: center; cursor: pointer;
    border: 2px dashed #d7d7cd; border-radius: 20px; margin: 2px;
    transition: background .15s, border-color .15s;
  }
  .drop:hover { background: #fbfbf9; }
  .drop.drag { background: var(--green-soft); border-color: var(--green); }
  .drop .icon {
    width: 56px; height: 56px; border-radius: 14px; background: #f1f1ec;
    display: grid; place-items: center; margin: 0 auto 16px;
  }
  .drop .icon svg { width: 26px; height: 26px; stroke: var(--ink); }
  .drop h2 { margin: 0 0 4px; font-size: 20px; font-weight: 700; }
  .drop .sub { color: var(--muted); font-size: 14px; }
  .drop .hint { color: var(--muted); font-size: 13px; margin-top: 10px; }
  .drop .file { color: var(--ink); font-weight: 600; margin-top: 10px; word-break: break-all; }

  .trust {
    display: flex; gap: 22px; justify-content: center; flex-wrap: wrap;
    color: var(--muted); font-size: 13.5px; margin-top: 22px;
  }
  .trust span { display: inline-flex; align-items: center; gap: 7px; }
  .trust svg { width: 15px; height: 15px; stroke: var(--muted); }

  .go {
    margin: 18px auto 0; display: block; width: 100%;
    font: inherit; font-weight: 700; font-size: 16px; color: #fff;
    background: var(--green); border: 0; border-radius: 14px; padding: 14px 18px;
    cursor: pointer;
  }
  .go:disabled { opacity: .55; cursor: not-allowed; }

  /* loading */
  .loading { text-align: center; padding: 56px 24px; }
  .spinner {
    width: 34px; height: 34px; border-radius: 50%;
    border: 3px solid var(--line); border-top-color: var(--green);
    margin: 0 auto 18px; animation: spin .8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .loading p { color: var(--muted); margin: 0; }

  /* results */
  .result { padding: 30px 28px 32px; }
  .score-head { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  .score-big { font-size: 44px; font-weight: 800; letter-spacing: -0.02em; }
  .score-big small { font-size: 20px; color: var(--muted); font-weight: 600; }
  .score-pill {
    font-size: 13px; font-weight: 700; padding: 4px 10px; border-radius: 999px;
  }
  .meter { height: 10px; border-radius: 999px; background: #eeeee7; overflow: hidden; margin: 16px 0 4px; }
  .meter > i { display: block; height: 100%; background: var(--green); border-radius: 999px; }
  .adj { color: var(--muted); font-size: 14px; margin-top: 6px; }

  .section-title { font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 26px 0 12px; }
  .cat { padding: 14px 0; border-top: 1px solid var(--line); }
  .cat:first-of-type { border-top: 0; }
  .cat .row { display: flex; justify-content: space-between; gap: 12px; font-weight: 650; }
  .cat .bar { height: 7px; border-radius: 999px; background: #eeeee7; overflow: hidden; margin: 8px 0; }
  .cat .bar > i { display: block; height: 100%; background: var(--green); }
  .cat .ev { color: var(--muted); font-size: 14px; }

  ul.clean { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
  ul.clean li { display: flex; gap: 10px; font-size: 15px; }
  ul.clean li::before { content: ""; flex: none; width: 18px; height: 18px; border-radius: 50%; margin-top: 2px; }
  ul.good li::before { background: var(--green-soft); box-shadow: inset 0 0 0 2px var(--green); }
  ul.work li::before { background: var(--amber-soft); box-shadow: inset 0 0 0 2px var(--amber); }

  .again { margin-top: 26px; background: none; border: 1px solid var(--line); color: var(--ink);
    font: inherit; font-weight: 600; padding: 11px 16px; border-radius: 12px; cursor: pointer; }
  .again:hover { border-color: #d8d8cf; }

  .err { color: var(--red); font-size: 14px; text-align: center; margin-top: 14px; min-height: 18px; }

  details.raw { margin-top: 22px; }
  details.raw summary { cursor: pointer; color: var(--muted); font-size: 13px; }
  details.raw pre { background: #fbfbf9; border: 1px solid var(--line); border-radius: 12px;
    padding: 14px; overflow: auto; font-size: 12px; line-height: 1.5; }

  footer { text-align: center; color: var(--muted); font-size: 13px; padding: 30px 0 50px; }
</style>
</head>
<body>
<header>
  <div class="wrap">
    <div class="brand"><span class="mark">R</span> Resume Reality Check</div>
    <a class="ghlink" href="https://github.com/Vineeth-Sagar/hiring-agent" target="_blank" rel="noopener">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="currentColor"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
      The algorithm
    </a>
  </div>
</header>

<main class="wrap">
  <div class="eyebrow">&#10022; Built on HackerRank&#39;s open-source hiring agent</div>
  <h1>What does an AI <em>actually&nbsp;think</em> of your resume?</h1>
  <p class="lede">Companies now run AI agents on your resume before a human ever sees it. Upload yours and get the same brutally honest score &mdash; in about a minute.</p>

  <form id="form">
    <div class="controls">
      <label for="role">Scoring against</label>
      <select id="role" name="role"><!--ROLE_OPTIONS--></select>
    </div>

    <div class="card">
      <div id="drop" class="drop" role="button" tabindex="0" aria-label="Upload a PDF resume">
        <div class="icon">
          <svg viewBox="0 0 24 24" fill="none" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M17 8l-5-5-5 5"/><path d="M12 3v13"/></svg>
        </div>
        <h2>Drop your resume here</h2>
        <div class="sub">or <span style="color:var(--green);font-weight:600">browse your files</span></div>
        <div class="hint">PDF only &middot; up to 12 MB</div>
        <div class="file" id="fileName" hidden></div>
      </div>
    </div>
    <input id="file" type="file" accept="application/pdf,.pdf" hidden />

    <button class="go" id="go" type="submit" disabled>Score my resume</button>
    <div class="err" id="err"></div>
  </form>

  <div class="trust">
    <span><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg> No sign-up, never stored</span>
    <span><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/></svg> Real recruiter rubric</span>
    <span><svg viewBox="0 0 24 24" fill="none" stroke-width="2"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.9a3.4 3.4 0 0 0-1-2.6c3-.3 6-1.5 6-6.6a5.1 5.1 0 0 0-1.4-3.6 4.8 4.8 0 0 0-.1-3.6s-1.1-.3-3.5 1.4a12 12 0 0 0-6.4 0C6.3 1.7 5.2 2 5.2 2a4.8 4.8 0 0 0-.1 3.6A5.1 5.1 0 0 0 3.7 9.2c0 5 3 6.3 6 6.6a3.4 3.4 0 0 0-1 2.6V22"/></svg> Structured, rubric-based scoring</span>
  </div>
</main>

<footer>Scores are generated by an LLM and are directional, not a hiring decision.</footer>

<script>
  const form = document.getElementById('form');
  const drop = document.getElementById('drop');
  const fileInput = document.getElementById('file');
  const fileName = document.getElementById('fileName');
  const go = document.getElementById('go');
  const err = document.getElementById('err');
  const roleSel = document.getElementById('role');
  const main = document.querySelector('main');
  let file = null;

  function setFile(f) {
    err.textContent = '';
    if (!f) return;
    if (f.type !== 'application/pdf' && !f.name.toLowerCase().endsWith('.pdf')) {
      err.textContent = 'Please choose a PDF file.'; return;
    }
    if (f.size > 12 * 1024 * 1024) { err.textContent = 'That PDF is over 12 MB.'; return; }
    file = f;
    fileName.hidden = false;
    fileName.textContent = f.name + '  (' + (f.size / 1024 / 1024).toFixed(1) + ' MB)';
    go.disabled = false;
  }

  drop.addEventListener('click', () => fileInput.click());
  drop.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileInput.click(); } });
  fileInput.addEventListener('change', () => setFile(fileInput.files[0]));
  ['dragenter', 'dragover'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove('drag'); }));
  drop.addEventListener('drop', e => { if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });

  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!file) return;
    err.textContent = '';
    main.innerHTML = '<div class="card"><div class="loading"><div class="spinner"></div>'
      + '<p>Reading your resume the way a hiring pipeline does&hellip;<br/>this usually takes about a minute.</p></div></div>';

    const fd = new FormData();
    fd.append('resume', file);
    fd.append('role', roleSel.value);

    try {
      const res = await fetch('/score', { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || ('Request failed (' + res.status + ')'));
      render(data);
    } catch (e2) {
      renderError(e2.message);
    }
  });

  function pill(pct) {
    if (pct >= 75) return ['Strong', 'var(--green-soft)', 'var(--green)'];
    if (pct >= 50) return ['Middling', 'var(--amber-soft)', 'var(--amber)'];
    return ['Weak', '#f6e6e6', 'var(--red)'];
  }

  function render(data) {
    const s = data.summary;
    const pct = s.max_score ? Math.round(100 * s.overall_score / s.max_score) : 0;
    const [plabel, pbg, pfg] = pill(pct);

    const cats = (s.categories || []).map(c => {
      const cpct = c.max ? Math.round(100 * c.score / c.max) : 0;
      return '<div class="cat">'
        + '<div class="row"><span>' + esc(c.icon ? c.icon + ' ' : '') + esc(c.label) + '</span><span>' + c.score + ' / ' + c.max + '</span></div>'
        + '<div class="bar"><i style="width:' + cpct + '%"></i></div>'
        + (c.evidence ? '<div class="ev">' + esc(c.evidence) + '</div>' : '')
        + '</div>';
    }).join('');

    const adj = [];
    if (s.bonus_points) adj.push('Bonus +' + s.bonus_points);
    if (s.deductions) adj.push('Deductions &minus;' + s.deductions);

    const strengths = (s.key_strengths || []).map(x => '<li>' + esc(x) + '</li>').join('');
    const improvements = (s.areas_for_improvement || []).map(x => '<li>' + esc(x) + '</li>').join('');

    main.innerHTML =
      '<div class="card"><div class="result">'
      + '<div class="score-head">'
      +   '<div class="score-big">' + s.overall_score + ' <small>/ ' + s.max_score + '</small></div>'
      +   '<span class="score-pill" style="background:' + pbg + ';color:' + pfg + '">' + plabel + ' &middot; ' + pct + '%</span>'
      + '</div>'
      + '<div class="meter"><i style="width:' + pct + '%"></i></div>'
      + (adj.length ? '<div class="adj">' + adj.join(' &nbsp;&middot;&nbsp; ') + '</div>' : '')
      + '<div style="color:var(--muted);font-size:14px;margin-top:6px">Scored against <strong>' + esc(data.role_label) + '</strong></div>'
      + (cats ? '<div class="section-title">Category breakdown</div>' + cats : '')
      + (strengths ? '<div class="section-title">Key strengths</div><ul class="clean good">' + strengths + '</ul>' : '')
      + (improvements ? '<div class="section-title">Areas for improvement</div><ul class="clean work">' + improvements + '</ul>' : '')
      + (s.bonus_breakdown ? '<div class="section-title">Bonus notes</div><div class="ev">' + esc(s.bonus_breakdown) + '</div>' : '')
      + (s.deduction_reasons && s.deductions ? '<div class="section-title">Deduction notes</div><div class="ev">' + esc(s.deduction_reasons) + '</div>' : '')
      + '<details class="raw"><summary>Raw JSON</summary><pre>' + esc(JSON.stringify(data, null, 2)) + '</pre></details>'
      + '<button class="again" onclick="location.reload()">Score another resume</button>'
      + '</div></div>';
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function renderError(msg) {
    main.innerHTML =
      '<div class="card"><div class="result">'
      + '<div class="score-head"><div class="score-big" style="color:var(--red)">Couldn&#39;t score that</div></div>'
      + '<p style="color:var(--muted)">' + esc(msg) + '</p>'
      + '<button class="again" onclick="location.reload()">Try another resume</button>'
      + '</div></div>';
  }
</script>
</body>
</html>"""
