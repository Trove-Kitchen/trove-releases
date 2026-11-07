"""Compare the import pipeline's two decision steps, today's rules against TypeSafe's Jev.

The two decisions:

  gate       "is this text a recipe?" (today: extract.is_plausible_recipe, before the model is called)
  grounding  "is this step supported by the caption, speech or on-screen text?"
             (today: extract.grounded_steps, after the model has written the recipe)

The data is the local lane's import training set in Trove-Kitchen/trove-dev-sessions
(evals/import-training-<date>/): each post's signals, and each recorded extraction
(inputs, raw model output) for Haiku 4.5 and Sonnet 5.5. Nothing is re-extracted:
the recorded model output is replayed through the resolver's own extract_recipe,
imported from a trove-app checkout, so "today" is exactly what that commit does.

  import_eval.py --resolver <trove-app>/services/video-resolver \
                 --data <dev-sessions>/evals/import-training-2026-11-07 \
                 --report <dev-sessions>/research/import-training-2026-11-07.md \
                 [--dry] [--model jev-1.14.0] --out-json agg.json --out-md summary.md

--dry runs today's side only and needs no key. Without --dry, TYPESAFE_API_KEY must be set.

This file runs in a PUBLIC repository's workflow. It prints and writes counts,
percentages, latencies and costs only: never captions, transcripts, recipe text or
post URLs. Errors print their type only. --detail writes per-decision rows (post id,
step index, label, decisions; no text) for a local look; the workflow never uses it.

Ground truth. The training set has no gold recipes yet (its README asks for
expected/<id>.json). So:
  * gate: a post holds a recipe when the report's results table marks recipe content in
    the text (Signals C, S or O) or gives it a score above 0. The table is read at run
    time from the report and never copied anywhere.
  * steps: a step the model wrote is "supported" when the relaxed rule the report
    describes and spot-checked keeps it (stems and prefix matches, number words to
    digits, numbers split from units, numbers strict, at most one unknown word in five).
    This is a silver label: the report checked every step the rule added back; the
    steps it rejects were not hand-checked.
  * gold wins: when expected/<id>.json exists, its ingredients/steps decide that post.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-1.14.0"  # pinned: what jev-latest resolves to (docs.typesafe.ai/models, Oct 2026)
PRICE_PER_MTOK = 0.042  # USD per million input tokens; output tokens are free (docs.typesafe.ai/models)
THRESHOLDS = (0.3, 0.5, 0.7)

GATE_QUESTION = {
    "type": "noul",
    "instructions": (
        "Does the state contain a cooking recipe: the ingredients or the method for "
        "making a specific dish?"
    ),
    "criteria": {
        "true": "Names ingredients or describes cooking steps for a dish, in the caption, "
                "the narration or the on-screen text",
        "false": "Only describes or praises a dish, lists hashtags, points to a recipe "
                 "elsewhere (\"recipe in bio\"), or is music, noise or unrelated talk",
    },
}

STEP_CRITERIA = {
    "true": "Every action, ingredient, time and temperature in the step is stated or clearly "
            "described in the state. Different word forms (blend/blended), digits for number "
            "words (3/three), units joined to numbers (15min) and mis-heard speech count as stated",
    "false": "The step adds an action, ingredient, time or temperature the state does not support",
}


# ---------------------------------------------------------------- loading


def load_extract(resolver: str):
    sys.path.insert(0, os.path.abspath(resolver))
    from app import extract  # noqa: E402  (trove-app services/video-resolver/app/extract.py)
    global _RESOLVER_CLEAN
    _RESOLVER_CLEAN = getattr(extract, "_clean", None)
    for name in ("is_plausible_recipe", "grounded_steps", "corpus", "extract_recipe",
                 "parse_model_json", "is_grounded", "ExtractError"):
        if not hasattr(extract, name):
            raise SystemExit(f"resolver has no extract.{name}")
    return extract


_RESOLVER_CLEAN = None


def clean(value, limit):
    """The resolver's own input trimming (extract._clean), with a copy if it is renamed."""
    if _RESOLVER_CLEAN is not None:
        return _RESOLVER_CLEAN(value, limit)
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def signals_of(inputs: dict) -> dict:
    """The four signals exactly as extract_recipe trims them."""
    on_screen = [line for line in (clean(x, 200) for x in (inputs.get("onScreenText") or [])[:80]) if line]
    return {
        "source_title": clean(inputs.get("sourceTitle"), 300),
        "caption": clean(inputs.get("caption"), 8000),
        "speech": clean(inputs.get("speech"), 12000),
        "on_screen": on_screen,
    }


def load_records(data: str) -> tuple[list[str], list[dict]]:
    posts = sorted(Path(p).name.split(".")[0] for p in glob.glob(os.path.join(data, "out", "*.signals.json")))
    records = []
    for path in sorted(glob.glob(os.path.join(data, "out", "*.json"))):
        parts = Path(path).name[:-len(".json")].split(".", 2)
        if len(parts) != 3 or parts[1] == "signals":
            continue
        post, variant, model = parts
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        records.append({"post": post, "variant": variant, "extractor": model, "data": rec})
    return posts, records


def gate_input(post: str, records: list[dict]) -> dict | None:
    """The inputs the phone sends for a post: the app (or TikTok caption) variant."""
    for variant in ("app", "caption"):
        for r in records:
            if r["post"] == post and r["variant"] == variant:
                return r["data"].get("inputs") or {}
    return None


# ---------------------------------------------------------------- ground truth

PLATFORM = {"youtube": "yt", "instagram": "ig", "tiktok": "tt"}
KIND = {"nigerian": "n", "regular": "r"}


def report_labels(report: str | None, posts: list[str]) -> dict[str, bool]:
    """post id -> holds a recipe in its text, from the report's results table."""
    if not report or not os.path.exists(report):
        return {}
    labels: dict[str, bool] = {}
    seen: dict[tuple[str, str], int] = {}
    with open(report, encoding="utf-8") as f:
        for line in f:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 9 or not cells[0].isdigit():
                continue
            plat = PLATFORM.get(cells[1].lower())
            kind = KIND.get(cells[2].lower())
            if not plat or not kind:
                continue
            seen[(plat, kind)] = seen.get((plat, kind), 0) + 1
            prefix = f"{plat}-{kind}{seen[(plat, kind)]}-"
            match = [p for p in posts if p.startswith(prefix)]
            if len(match) != 1:
                continue
            signal_tokens = set(re.findall(r"[A-Za-z]+", cells[5]))
            scores = [int(x) for x in re.findall(r"\d", cells[7])]
            text_score = scores[-1] if scores else 0  # TikTok: the caption-only score in brackets
            labels[match[0]] = bool(signal_tokens & {"C", "S", "O"}) or text_score > 0
    return labels


WORDNUM = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
    "half": "1/2", "quarter": "1/4",
}
FRACTIONS = {"½": "1/2", "¼": "1/4", "¾": "3/4"}
FILLER = {
    "the", "and", "for", "with", "into", "from", "your", "that", "this", "then", "until",
    "about", "just", "some", "very", "over", "under", "onto", "also", "well", "back", "down",
    "when", "once", "have", "been", "them", "they", "you", "are", "was", "were", "will",
    "can", "not", "but", "all", "any", "its",
}
SUFFIXES = ("ing", "ed", "es", "s", "ly", "er", "en")
NUMBER = re.compile(r"\d+(?:\.\d+|/\d+)?")
WORDNUM_RE = re.compile(r"\b(" + "|".join(WORDNUM) + r")\b")


def normalize(text: str) -> str:
    text = text.lower()
    for k, v in FRACTIONS.items():
        text = text.replace(k, f" {v} ")
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text)
    return WORDNUM_RE.sub(lambda m: WORDNUM[m.group(1)], text)


def stem(word: str) -> str:
    for s in SUFFIXES:
        if word.endswith(s) and len(word) - len(s) >= 3:
            return word[:-len(s)]
    return word


def has_number(haystack: str, number: str) -> bool:
    return re.search(rf"(?<![0-9])(?<![0-9][./]){re.escape(number)}(?![./][0-9])(?![0-9])", haystack) is not None


class SourceIndex:
    def __init__(self, source: str):
        self.text = normalize(source)
        self.tokens = set(re.findall(r"[a-z]+", self.text))
        self.stems = {stem(t) for t in self.tokens}

    def word_ok(self, word: str) -> bool:
        st = stem(word)
        if word in self.tokens or st in self.stems:
            return True
        prefix = st[:max(4, len(st) - 1)] if len(st) >= 4 else st
        return any(t.startswith(prefix) or (len(t) >= 4 and st.startswith(stem(t))) for t in self.tokens)


def silver_supported(step: str, index: SourceIndex) -> bool:
    """The report's relaxed step rule (research/import-training-2026-11-07.md, Patterns)."""
    s = normalize(step)
    numbers = NUMBER.findall(s)
    words = [w for w in re.findall(r"[a-z]+", s) if len(w) >= 3 and w not in FILLER]
    if not words and not numbers:
        return False
    if any(not has_number(index.text, n) for n in numbers):
        return False
    unknown = sum(1 for w in words if not index.word_ok(w))
    return unknown <= len(words) // 5


def gold_supported(step: str, gold_steps: list[str]) -> bool:
    mine = {stem(w) for w in re.findall(r"[a-z0-9/]+", normalize(step)) if w not in FILLER}
    if not mine:
        return False
    for g in gold_steps:
        theirs = {stem(w) for w in re.findall(r"[a-z0-9/]+", normalize(g)) if w not in FILLER}
        if len(mine & theirs) / len(mine) >= 0.6:
            return True
    return False


def load_gold(data: str, post: str) -> dict | None:
    path = os.path.join(data, "expected", f"{post}.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- Jev


class Jev:
    def __init__(self, key: str, model: str):
        self.key = key
        self.model = model
        self.resolved: set[str] = set()
        self.input_tokens = 0
        self.output_tokens = 0
        self.requests = 0
        self.retries = 0
        self.failures = 0

    def ask(self, state: dict, questions: dict) -> tuple[dict | None, float]:
        body = json.dumps({"model": self.model, "state": state, "questions": questions}).encode()
        for attempt in range(6):
            req = urllib.request.Request(TYPESAFE_URL, data=body, method="POST", headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            })
            start = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.load(resp)
                ms = (time.perf_counter() - start) * 1000
            except urllib.error.HTTPError as exc:
                # Never print the body: a 422 can echo the request.
                if exc.code in (429, 500, 502, 503, 504, 529) and attempt < 5:
                    self.retries += 1
                    wait = exc.headers.get("retry-after") if exc.headers else None
                    time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else 2 ** attempt)
                    continue
                print(f"jev request failed: HTTP {exc.code}")
                self.failures += 1
                return None, 0.0
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt < 5:
                    self.retries += 1
                    time.sleep(2 ** attempt)
                    continue
                print(f"jev request failed: {type(exc).__name__}")
                self.failures += 1
                return None, 0.0
            self.requests += 1
            usage = data.get("usage") or {}
            self.input_tokens += int(usage.get("input_tokens") or 0)
            self.output_tokens += int(usage.get("output_tokens") or 0)
            if data.get("model"):
                self.resolved.add(str(data["model"]))
            answers = {k: v.get("noul") for k, v in (data.get("answers") or {}).items() if isinstance(v, dict)}
            return answers, ms
        return None, 0.0


def jev_state(sig: dict) -> dict:
    state: dict = {}
    if sig["source_title"]:
        state["source_title"] = sig["source_title"]
    if sig["caption"]:
        state["post_caption"] = sig["caption"]
    if sig["speech"]:
        state["spoken_narration (auto-transcribed; names and numbers may be mis-heard)"] = sig["speech"]
    if sig["on_screen"]:
        state["on_screen_text (OCR; often watermarks or appliance labels)"] = sig["on_screen"]
    return state or {"post_caption": ""}


def step_question(step: str) -> dict:
    return {
        "type": "noul",
        "instructions": {"question": "Is this cooking step supported by the state?", "step": step},
        "criteria": STEP_CRITERIA,
    }


# ---------------------------------------------------------------- metrics


def pct(a: int, b: int) -> float | None:
    return round(100.0 * a / b, 1) if b else None


def quantiles(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "p50": None, "p95": None}
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, max(0, round(0.95 * len(ordered)) - 1))]
    return {"n": len(values), "p50": round(statistics.median(ordered), 3), "p95": round(p95, 3)}


def gate_metrics(rows: list[tuple[bool, bool]]) -> dict:
    tp = sum(1 for t, p in rows if t and p)
    fp = sum(1 for t, p in rows if not t and p)
    fn = sum(1 for t, p in rows if t and not p)
    tn = sum(1 for t, p in rows if not t and not p)
    return {"n": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision_pct": pct(tp, tp + fp), "recall_pct": pct(tp, tp + fn),
            "accuracy_pct": pct(tp + tn, len(rows))}


def step_metrics(rows: list[tuple[bool, bool]]) -> dict:
    m = gate_metrics(rows)
    return {"n": m["n"], "supported": m["tp"] + m["fn"],
            "kept_correct": m["tp"], "wrongly_dropped": m["fn"],
            "wrongly_kept": m["fp"], "rightly_dropped": m["tn"],
            "precision_pct": m["precision_pct"], "recall_pct": m["recall_pct"],
            "accuracy_pct": m["accuracy_pct"]}


# ---------------------------------------------------------------- run


def run(args) -> dict:
    X = load_extract(args.resolver)
    posts, records = load_records(args.data)
    from_report = report_labels(args.report, posts)

    jev = None
    if not args.dry:
        key = os.environ.get("TYPESAFE_API_KEY", "").strip()
        if not key:
            raise SystemExit("TYPESAFE_API_KEY is not set (use --dry for today's rules only)")
        jev = Jev(key, args.model)

    # ---- gate, one decision per post on the inputs the phone sends
    truth: dict[str, bool] = {}
    label_source = {"gold": 0, "report": 0, "none": 0}
    gate_now, gate_jev, gate_prob = {}, {}, {}
    lat_now_gate, lat_jev_gate = [], []
    recorded_agree = 0
    for post in posts:
        gold = load_gold(args.data, post)
        if gold is not None:
            truth[post] = bool(gold.get("ingredients") or gold.get("steps"))
            label_source["gold"] += 1
        elif post in from_report:
            truth[post] = from_report[post]
            label_source["report"] += 1
        else:
            label_source["none"] += 1
        inputs = gate_input(post, records)
        if inputs is None:
            continue
        sig = signals_of(inputs)
        text = X.corpus(sig["source_title"], sig["caption"], sig["speech"], sig["on_screen"])
        t0 = time.perf_counter()
        gate_now[post] = bool(X.is_plausible_recipe(text))
        lat_now_gate.append((time.perf_counter() - t0) * 1000)
        if jev is not None:
            answers, ms = jev.ask(jev_state(sig), {"is_recipe": GATE_QUESTION})
            if answers is not None and answers.get("is_recipe") is not None:
                gate_prob[post] = float(answers["is_recipe"])
                gate_jev[post] = gate_prob[post] >= args.threshold
                lat_jev_gate.append(ms)
    for r in records:
        if r["variant"] in ("app", "caption") and r["post"] in gate_now:
            recorded_agree += int(bool((r["data"].get("gate") or {}).get("pass")) == gate_now[r["post"]])

    # ---- grounding, one decision per step the model wrote, per recorded extraction
    step_rows_now, step_rows_jev = [], []
    jev_probs: list[tuple[bool, float]] = []
    by_extractor: dict[str, dict[str, list]] = {}
    lat_now_step, lat_jev_req, steps_per_req = [], [], []
    step_label_source = {"gold": 0, "silver": 0}
    strict_kept_silver_negative = 0
    e2e = {"now": 0, "jev": 0, "n": 0}
    detail = []
    for r in records:
        rec = r["data"]
        post = r["post"]
        sig = signals_of(rec.get("inputs") or {})
        text = X.corpus(sig["source_title"], sig["caption"], sig["speech"], sig["on_screen"])
        lowered = text.lower()
        raw = rec.get("raw_model")
        parsed = X.parse_model_json(raw) if isinstance(raw, str) else None
        steps = list(parsed["steps"]) if parsed else []
        gold = load_gold(args.data, post)
        index = SourceIndex(text)
        labels = []
        for s in steps:
            if gold is not None:
                labels.append(gold_supported(s, gold.get("steps") or []))
                step_label_source["gold"] += 1
            else:
                labels.append(silver_supported(s, index))
                step_label_source["silver"] += 1

        # Today's grounding, one decision at a time (for latency), then the real pipeline end to end.
        now_keep = []
        for s in steps:
            t0 = time.perf_counter()
            now_keep.append(bool(X.grounded_steps([s], lowered)))
            lat_now_step.append((time.perf_counter() - t0) * 1000)
        strict_kept_silver_negative += sum(1 for k, lab in zip(now_keep, labels) if k and not lab)
        try:
            out = X.extract_recipe(sig["source_title"], sig["caption"], sig["speech"], sig["on_screen"],
                                   lambda _prompt, _raw=raw: _raw if isinstance(_raw, str) else "")
        except X.ExtractError:
            out = None

        jev_keep: list[bool | None] = [None] * len(steps)
        if jev is not None and steps:
            questions = {f"step_{i}": step_question(s) for i, s in enumerate(steps)}
            answers, ms = jev.ask(jev_state(sig), questions)
            if answers is not None:
                lat_jev_req.append(ms)
                steps_per_req.append(len(steps))
                for i in range(len(steps)):
                    p = answers.get(f"step_{i}")
                    if p is not None:
                        jev_keep[i] = float(p) >= args.threshold
                        jev_probs.append((labels[i], float(p)))

        bucket = by_extractor.setdefault(r["extractor"], {"now": [], "jev": []})
        for i, lab in enumerate(labels):
            step_rows_now.append((lab, now_keep[i]))
            bucket["now"].append((lab, now_keep[i]))
            if jev_keep[i] is not None:
                step_rows_jev.append((lab, jev_keep[i]))
                bucket["jev"].append((lab, jev_keep[i]))

        # End to end (proxy): a post without a recipe imports nothing; a post with one imports a
        # recipe whose kept steps are exactly the supported ones. Ingredient grounding stays today's.
        if post in truth:
            e2e["n"] += 1
            positive = truth[post]
            now_ok = (out is None) if not positive else (
                out is not None and [s for s, k in zip(steps, now_keep) if k] == [s for s, l in zip(steps, labels) if l])
            e2e["now"] += int(now_ok)
            if jev is not None:
                gate_pass = gate_jev.get(post)
                imported = bool(gate_pass) and parsed is not None and X.is_grounded(parsed["ingredients"], lowered)
                if gate_pass is None or any(k is None for k in jev_keep):
                    jev_ok = False
                elif not positive:
                    jev_ok = not imported
                else:
                    jev_ok = imported and all(k == l for k, l in zip(jev_keep, labels))
                e2e["jev"] += int(jev_ok)
        if args.detail:
            for i, lab in enumerate(labels):
                detail.append({"post": post, "variant": r["variant"], "extractor": r["extractor"],
                               "step": i, "supported": lab, "now": now_keep[i], "jev": jev_keep[i]})

    gate_rows_now = [(truth[p], gate_now[p]) for p in gate_now if p in truth]
    gate_rows_jev = [(truth[p], gate_jev[p]) for p in gate_jev if p in truth]
    result = {
        "dataset": os.path.basename(os.path.normpath(args.data)),
        "mode": "dry" if jev is None else "jev",
        "jev_model_requested": None if jev is None else args.model,
        "jev_model_answered": None if jev is None else sorted(jev.resolved),
        "threshold": args.threshold,
        "posts": len(posts),
        "extractions": len(records),
        "ground_truth": {
            "gate_labels": label_source,
            "step_labels": step_label_source,
            "gate_positive": sum(1 for v in truth.values() if v),
            "gate_negative": sum(1 for v in truth.values() if not v),
            "strict_kept_but_silver_unsupported": strict_kept_silver_negative,
        },
        "gate": {
            "now": gate_metrics(gate_rows_now),
            "now_matches_recorded_run": f"{recorded_agree} of {sum(1 for r in records if r['variant'] in ('app', 'caption'))}",
            "jev": gate_metrics(gate_rows_jev) if jev else None,
        },
        "steps": {
            "now": step_metrics(step_rows_now),
            "jev": step_metrics(step_rows_jev) if jev else None,
            "by_extractor": {k: {"now": step_metrics(v["now"]), "jev": step_metrics(v["jev"]) if jev else None}
                             for k, v in sorted(by_extractor.items())},
            "jev_threshold_sweep": {str(t): step_metrics([(l, p >= t) for l, p in jev_probs])
                                    for t in THRESHOLDS} if jev else None,
        },
        "import_right_proxy": {"n": e2e["n"], "now": e2e["now"], "jev": e2e["jev"] if jev else None},
        "latency_ms": {
            "now_gate_per_decision": quantiles(lat_now_gate),
            "now_step_per_decision": quantiles(lat_now_step),
            "jev_gate_per_request": quantiles(lat_jev_gate),
            "jev_steps_per_request": quantiles(lat_jev_req),
            "jev_steps_per_request_decisions": quantiles([float(x) for x in steps_per_req]),
        },
        "cost": None if jev is None else {
            "requests": jev.requests, "retries": jev.retries, "failed_requests": jev.failures,
            "input_tokens": jev.input_tokens, "output_tokens": jev.output_tokens,
            "usd": round(jev.input_tokens * args.price_per_mtok / 1e6, 6),
            "usd_per_post": round(jev.input_tokens * args.price_per_mtok / 1e6 / max(1, len(posts)), 8),
            "price_per_mtok_input": args.price_per_mtok,
        },
    }
    if args.detail:
        Path(args.detail).write_text(json.dumps(detail, indent=1), encoding="utf-8")
    return result


def fmt(v, suffix=""):
    return "n/a" if v is None else f"{v}{suffix}"


def markdown(r: dict) -> str:
    g, s, lat = r["gate"], r["steps"], r["latency_ms"]
    jg, js = g["jev"] or {}, s["jev"] or {}
    lines = [
        f"## Import decisions: today vs Jev ({r['mode']} run)",
        "",
        f"Dataset `{r['dataset']}`: {r['posts']} posts, {r['extractions']} recorded extractions. "
        f"Jev model: {fmt(r['jev_model_requested'])} (answered by {', '.join(r['jev_model_answered'] or []) or 'n/a'}), "
        f"threshold {r['threshold']}.",
        "",
        "| Decision | Metric | Today | Jev |",
        "|---|---|---|---|",
        f"| Gate | precision | {fmt(g['now']['precision_pct'], '%')} | {fmt(jg.get('precision_pct'), '%')} |",
        f"| Gate | recall | {fmt(g['now']['recall_pct'], '%')} | {fmt(jg.get('recall_pct'), '%')} |",
        f"| Gate | TP / FP / FN / TN | {g['now']['tp']} / {g['now']['fp']} / {g['now']['fn']} / {g['now']['tn']} | "
        + (f"{jg['tp']} / {jg['fp']} / {jg['fn']} / {jg['tn']}" if jg else "n/a") + " |",
        f"| Steps | supported steps kept | {s['now']['kept_correct']} of {s['now']['supported']} | "
        + (f"{js['kept_correct']} of {js['supported']}" if js else "n/a") + " |",
        f"| Steps | supported steps wrongly dropped | {s['now']['wrongly_dropped']} | {fmt(js.get('wrongly_dropped'))} |",
        f"| Steps | unsupported steps wrongly kept | {s['now']['wrongly_kept']} | {fmt(js.get('wrongly_kept'))} |",
        f"| Steps | unsupported steps dropped | {s['now']['rightly_dropped']} | {fmt(js.get('rightly_dropped'))} |",
        f"| Steps | recall / precision | {fmt(s['now']['recall_pct'], '%')} / {fmt(s['now']['precision_pct'], '%')} | "
        f"{fmt(js.get('recall_pct'), '%')} / {fmt(js.get('precision_pct'), '%')} |",
        f"| End to end | imports right (proxy) | {r['import_right_proxy']['now']} of {r['import_right_proxy']['n']} | "
        f"{fmt(r['import_right_proxy']['jev'])} of {r['import_right_proxy']['n']} |",
        f"| Latency | gate p50 / p95 | {fmt(lat['now_gate_per_decision']['p50'])} / {fmt(lat['now_gate_per_decision']['p95'])} ms | "
        f"{fmt(lat['jev_gate_per_request']['p50'])} / {fmt(lat['jev_gate_per_request']['p95'])} ms |",
        f"| Latency | steps p50 / p95 | {fmt(lat['now_step_per_decision']['p50'])} / {fmt(lat['now_step_per_decision']['p95'])} ms per step | "
        f"{fmt(lat['jev_steps_per_request']['p50'])} / {fmt(lat['jev_steps_per_request']['p95'])} ms per request "
        f"(median {fmt(lat['jev_steps_per_request_decisions']['p50'])} steps each) |",
    ]
    if r["cost"]:
        c = r["cost"]
        lines.append(f"| Cost | tokens in / out, USD | 0 | {c['input_tokens']} / {c['output_tokens']}, "
                     f"${c['usd']:.6f} (${c['usd_per_post']:.6f} per post) |")
    gt = r["ground_truth"]
    lines += [
        "",
        f"Ground truth: gate labels from {gt['gate_labels']['gold']} gold files and {gt['gate_labels']['report']} "
        f"report grades ({gt['gate_positive']} recipe, {gt['gate_negative']} not); step labels "
        f"{gt['step_labels']['gold']} gold, {gt['step_labels']['silver']} silver (the report's relaxed rule). "
        f"Today's gate matches the recorded run on {g['now_matches_recorded_run']} extractions. "
        "Today's ingredient check is used on both sides.",
    ]
    if s["jev_threshold_sweep"]:
        lines += ["", "| Jev step threshold | kept correct | wrongly dropped | wrongly kept |", "|---|---|---|---|"]
        for t, m in s["jev_threshold_sweep"].items():
            lines.append(f"| {t} | {m['kept_correct']} | {m['wrongly_dropped']} | {m['wrongly_kept']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--resolver", required=True, help="trove-app services/video-resolver")
    ap.add_argument("--data", required=True, help="dev-sessions evals/import-training-<date>")
    ap.add_argument("--report", help="dev-sessions research/import-training-<date>.md (gate labels)")
    ap.add_argument("--dry", action="store_true", help="today's rules only; no key needed")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--threshold", type=float, default=0.5, help="Jev noul at or above this is yes")
    ap.add_argument("--price-per-mtok", type=float, default=PRICE_PER_MTOK)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--detail", help="local only: per-decision rows (ids and decisions, no text)")
    ap.add_argument("--debug", action="store_true", help="local only: full tracebacks")
    args = ap.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{0,63}", args.model):
        print("model must look like jev-1.14.0 or jev-latest")
        return 2
    try:
        result = run(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a traceback could quote training text
        if args.debug:
            raise
        print(f"import eval failed: {type(exc).__name__}")
        return 1
    Path(args.out_json).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    md = markdown(result)
    Path(args.out_md).write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
