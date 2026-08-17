"""TritonAI connection smoke test (Part 2 §5 of TRITONAI_SETUP.md).

Run this the moment you paste your key into .env:

    python smoke_test.py

Lists the models the proxy will serve you, then makes one tiny chat call and one
structured-output call so both code paths the prototype uses are proven.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()  # must happen before importing connect (script, not notebook)

from pydantic import BaseModel  # noqa: E402

from utils.connect import DEFAULT_MODEL, ask, ask_json, list_models  # noqa: E402


class Ping(BaseModel):
    status: str
    model_family: str


def main() -> int:
    print(f"project default model: {DEFAULT_MODEL}\n")

    try:
        models = list_models()
    except ValueError as exc:
        # Missing key — the one failure worth a friendly message.
        print(f"error: {exc}", file=sys.stderr)
        print("\nPaste your key into .env as TRITONAI_API_KEY=... and rerun.", file=sys.stderr)
        return 1

    print(f"{len(models)} models available:")
    for m in models:
        marker = "  <- project default" if m["id"] == DEFAULT_MODEL else ""
        print(f"  {m['id']:<28} {m['type']}{marker}")

    if not any(m["id"] == DEFAULT_MODEL for m in models):
        sys.stdout.flush()  # keep the warning below the model list, not above it
        print(
            f"\nwarning: {DEFAULT_MODEL!r} is not in the list above. "
            "Check https://tritonai-api.ucsd.edu/ui/?page=models and update "
            "DEFAULT_MODEL in utils/connect.py.",
            file=sys.stderr,
        )

    print("\n--- ask() ---")
    print(ask("Reply with exactly: connection ok", verbose=True))

    print("\n--- ask_json(schema=...) ---")
    # The no-fence line is required: TritonAI accepts response_format=json_object
    # but does not enforce it, so models happily return ```json ... ``` which
    # fails Pydantic validation. The instruction has to come from the prompt.
    ping = ask_json(
        "Report status 'ok' and the family of the model answering this.",
        schema=Ping,
        system=(
            "You are a helpful assistant. Be concise. Return ONLY raw JSON. "
            "Do not wrap it in markdown code fences and do not add prose."
        ),
        verbose=True,
    )
    print(f"status={ping.status!r} model_family={ping.model_family!r}")

    print("\nboth paths work. Next: python generate_summaries.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
