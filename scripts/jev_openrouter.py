#!/usr/bin/env python3
"""
jev_openrouter.py -- ask Jev through OpenRouter when TypeSafe is unavailable.

Uses OpenRouter's Decisions API with the same typed request body as TypeSafe.
The API key comes from OPENROUTER_API_KEY or ~/.pi/agent/auth.json.
jev-refine.py and delegate.py use this module when TypeSafe fails.
"""

import concurrent.futures
import json
import os
import sys
import urllib.request

ENDPOINT = os.environ.get("JEV_OPENROUTER_ENDPOINT", "https://openrouter.ai/api/alpha/decisions")
MODEL = os.environ.get("JEV_OPENROUTER_MODEL", "typesafe/jev-1.13")
TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "8"))
PROVIDER = "openrouter"


def api_key() -> str:
    """Return the OpenRouter key from the environment or pi's auth file."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    path = os.path.expanduser("~/.pi/agent/auth.json")
    try:
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError, TypeError):
        return ""
    provider = record.get(PROVIDER, {}) if isinstance(record, dict) else {}
    key = provider.get("key", "") if isinstance(provider, dict) else ""
    return key if isinstance(key, str) else ""


def available() -> bool:
    return bool(api_key())


def ask(body: dict, timeout: float = TIMEOUT) -> dict:
    """Send one typed Decisions API request and return its JSON response."""
    key = api_key()
    if not key:
        raise RuntimeError("no OpenRouter key")
    request_body = dict(body)
    request_body["model"] = MODEL
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(request_body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "jev-interceptor/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def rank(query: str, candidates: list[str], instructions: str, top: int = 4,
         min_p: float = 0.75, timeout: float = 6.0) -> list[tuple[str, float]]:
    """Score skill candidates independently, in concurrent batches of 120."""
    chunks = [candidates[i:i + 120] for i in range(0, len(candidates), 120)]
    if not chunks:
        return []

    def score_chunk(chunk: list[str]) -> list[tuple[str, float]]:
        # Same shape as `jev rank`: each question carries its own item, keyed as
        # `candidate` inside structured instructions (Jev mis-locates items by index).
        question = instructions or "Is `candidate` relevant to `query`?"
        questions = {f"c{i}": {"type": "noul", "instructions": {"question": question, "candidate": candidate}}
                     for i, candidate in enumerate(chunk)}
        result = ask({"state": {"query": query[:6000]}, "questions": questions}, timeout=timeout)
        answers = result.get("answers") or {}
        ranked = []
        for i, candidate in enumerate(chunk):
            answer = answers.get(f"c{i}") or {}
            if answer.get("type") != "noul":
                continue
            try:
                probability = float(answer["noul"])
            except (KeyError, TypeError, ValueError):
                continue
            if probability >= min_p:
                ranked.append((candidate, probability))
        return ranked

    ranked: list[tuple[str, float]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(chunks))) as pool:
        futures = [pool.submit(score_chunk, chunk) for chunk in chunks]
        for future in concurrent.futures.as_completed(futures):
            try:
                ranked.extend(future.result())
            except Exception:
                continue
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:max(0, top)]


def selftest() -> int:
    if not available():
        print("FAIL no OpenRouter key")
        return 1
    try:
        result = ask({
            "state": {"text": "The build failed with a syntax error on line 3."},
            "questions": {
                "is_error": {
                    "type": "noul",
                    "instructions": "Does `text` describe an error?",
                },
            },
        })
        answer = result["answers"]["is_error"]
        probability = float(answer["noul"])
        cost = result.get("usage", {}).get("cost", 0)
        print(f"OK p={probability:g} cost=${cost}")
        return 0
    except Exception as error:
        message = str(error)
        key = api_key()
        if key:
            message = message.replace(key, "[redacted]")
        print(f"FAIL {type(error).__name__}: {message}")
        return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["selftest"]:
        sys.exit(selftest())
    print("Usage: python3 jev_openrouter.py selftest")
    sys.exit(2)
