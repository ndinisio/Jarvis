"""Model bake-off: which local model should JARVIS run on this Mac?

    PYTHONPATH=backend python -m evals.bake_off                 # default candidates
    PYTHONPATH=backend python -m evals.bake_off --models qwen3:4b,qwen3:8b --pull

For each candidate, JARVIS is configured to use that one model for every
text slot (one resident model is what keeps a 16 GB Mac responsive), then
the understanding corpus and the web suite run against it. Memory is read
from Ollama's own report of the loaded model. The table at the end is what
the defaults are chosen from — measured, not assumed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from . import run_understanding, run_web
from .suites import ROOT

DEFAULT_CANDIDATES = ("qwen3:4b", "qwen3:8b", "qwen3-vl:4b", "qwen3-vl:8b", "gemma3:4b", "llama3.1:8b")


def ollama(base_url: str, path: str, **kwargs):
    return httpx.request(kwargs.pop("method", "GET"), f"{base_url.rstrip('/')}{path}",
                         timeout=kwargs.pop("timeout", 10.0), **kwargs)


def installed(base_url: str) -> set[str]:
    try:
        return {m["name"] for m in ollama(base_url, "/api/tags").json().get("models", [])}
    except httpx.HTTPError:
        return set()


def pull(base_url: str, model: str) -> bool:
    print(f"pulling {model}…", flush=True)
    try:
        response = ollama(base_url, "/api/pull", method="POST", json={"model": model, "stream": False},
                          timeout=3600.0)
        return response.status_code == 200
    except httpx.HTTPError as exc:
        print(f"  pull failed: {exc}")
        return False


def resident_gb(base_url: str, model: str) -> float | None:
    try:
        for entry in ollama(base_url, "/api/ps").json().get("models", []):
            if entry.get("name") == model or entry.get("model") == model:
                return round(entry.get("size", 0) / 1e9, 2)
    except httpx.HTTPError:
        return None
    return None


def overrides_for(model: str) -> list[str]:
    return [f"models.{slot}.model={model}" for slot in ("fast", "general", "reasoning")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=",".join(DEFAULT_CANDIDATES))
    parser.add_argument("--ollama", default="http://localhost:11434")
    parser.add_argument("--pull", action="store_true", help="download candidates that aren't installed")
    parser.add_argument("--max-phase", type=int, default=None)
    parser.add_argument("--skip-web", action="store_true")
    parser.add_argument("--skip-understanding", action="store_true")
    args = parser.parse_args(argv)

    have = installed(args.ollama)
    if not have and not args.pull:
        print(f"Ollama isn't answering at {args.ollama} (or has no models). Start it with `ollama serve`.")
        return 2
    rows = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        if model not in have and not (args.pull and pull(args.ollama, model)):
            print(f"skipping {model}: not installed (use --pull)")
            continue
        sets = overrides_for(model)
        row = {"model": model}
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = model.replace(":", "_").replace("/", "_")
        if not args.skip_understanding:
            out = ROOT / "results" / f"bakeoff-understanding-{safe}-{stamp}.json"
            run_understanding.main(["--model", "real", "--quiet", "--out", str(out),
                                    *sum((["--set", s] for s in sets), [])])
            summary = json.loads(out.read_text())["summary"]
            row["chat_act"] = summary["mode"]["rate"]
            row["semantic_p50_ms"] = summary["semantic_p50_ms"]
        if not args.skip_web:
            out = ROOT / "results" / f"bakeoff-web-{safe}-{stamp}.json"
            web_args = ["--model", "real", "--out", str(out), "--label", f"bakeoff:{model}",
                        *sum((["--set", s] for s in sets), [])]
            if args.max_phase is not None:
                web_args += ["--max-phase", str(args.max_phase)]
            run_web.main(web_args)
            summary = json.loads(out.read_text())["summary"]
            row["web_success"] = summary["success_rate"]
            row["web_p50_s"] = summary["p50_wall_s_passed"]
            row["model_calls"] = summary["mean_model_calls"]
        row["resident_gb"] = resident_gb(args.ollama, model)
        rows.append(row)

    print(f"\n{'model':<16} {'chat/act':>9} {'sem p50':>9} {'web':>7} {'web p50':>8} {'calls':>6} {'GB':>6}")
    for row in rows:
        print(f"{row['model']:<16} {_pct(row.get('chat_act')):>9} {_num(row.get('semantic_p50_ms'), 'ms'):>9} "
              f"{_pct(row.get('web_success')):>7} {_num(row.get('web_p50_s'), 's'):>8} "
              f"{_num(row.get('model_calls')):>6} {_num(row.get('resident_gb')):>6}")
    out = ROOT / "results" / f"bakeoff-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"suite": "bakeoff", "rows": rows}, indent=2), encoding="utf-8")
    print(f"results written to {out}")
    return 0


def _pct(value) -> str:
    return "—" if value is None else f"{value:.0%}"


def _num(value, unit: str = "") -> str:
    return "—" if value is None else f"{value:.0f}{unit}" if unit == "ms" else f"{value:.1f}{unit}"


if __name__ == "__main__":
    sys.exit(main())
