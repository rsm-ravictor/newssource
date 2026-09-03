"""Measured API usage for one run: Tavily requests and LLM tokens.

Estimates and actuals answer different questions. Before a run you want "what
will this cost me", which is arithmetic over the roster. Afterwards you want
"what did it cost me", and that can only come from the providers: how many
articles a query returns varies, and prompt size varies with it.

Getting the real numbers is awkward for one reason - ``utils/connect.py`` is
verbatim-locked and throws ``resp.usage`` away, returning only text. But it takes
a ``client`` argument, and that injection point is the seam: pass it a client
that records what the provider reported on the way through, and the token counts
are exact rather than guessed at, with no edit to the locked file. The Tavily
client is wrapped the same way.

    meter = Meter()
    llm = meter.wrap_llm(get_client())
    tavily = meter.wrap_tavily(search_mod.get_client())
    ...
    meter.totals()   # {'llm_calls': 63, 'input_tokens': 412_889, ...}

A wrapper must never be the reason a run fails, so every recording path is
defensive: an unexpected usage shape is skipped, not raised.
"""

from __future__ import annotations

import threading


class Meter:
    """Running totals for one pipeline run. Safe to share across threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.llm_failed = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.tavily_queries = 0
        self.tavily_failed = 0
        self.articles_returned = 0

    # -- recording ---------------------------------------------------------

    def record_llm(self, response) -> None:
        """Add one completion's reported usage.

        Reads the provider's own numbers rather than counting tokens locally: a
        local tokenizer would disagree with the bill, and the point of this is to
        match what the account is charged for. TritonAI proxies several backends
        and not all of them report every field, so each is read independently.
        """
        usage = getattr(response, "usage", None)
        with self._lock:
            self.llm_calls += 1
            if usage is None:
                return
            self.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
            details = getattr(usage, "prompt_tokens_details", None)
            self.cached_tokens += int(getattr(details, "cached_tokens", 0) or 0)

    def record_llm_failure(self) -> None:
        with self._lock:
            self.llm_calls += 1
            self.llm_failed += 1

    def record_search(self, response) -> None:
        """One billed Tavily request, plus how many results it actually returned."""
        results = []
        if isinstance(response, dict):
            results = response.get("results") or []
        with self._lock:
            self.tavily_queries += 1
            self.articles_returned += len(results)

    def record_search_failure(self) -> None:
        """A request that raised. Counted separately: it may not be billed."""
        with self._lock:
            self.tavily_failed += 1

    # -- reading -----------------------------------------------------------

    def totals(self) -> dict:
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "llm_failed": self.llm_failed,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_tokens": self.cached_tokens,
                "total_tokens": self.input_tokens + self.output_tokens,
                "tavily_queries": self.tavily_queries,
                "tavily_failed": self.tavily_failed,
                "articles_returned": self.articles_returned,
            }

    # -- wrapping ----------------------------------------------------------

    def wrap_llm(self, client):
        """An OpenAI-compatible client that records usage as calls pass through."""
        return _MeteredLLM(client, self)

    def wrap_tavily(self, client):
        """A Tavily client that counts requests as they pass through."""
        return _MeteredTavily(client, self)


class _Proxy:
    """Forwards every attribute it does not define to the wrapped object.

    Subclasses intercept one method each. Anything else about the client - other
    endpoints, attributes a future connect.py might reach for - keeps working
    untouched, so wrapping cannot narrow what the caller can do.
    """

    def __init__(self, inner, meter: Meter) -> None:
        # Bypass __getattr__ for our own two fields.
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_meter", meter)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)


class _MeteredLLM(_Proxy):
    @property
    def chat(self):
        return _MeteredChat(self._inner.chat, self._meter)


class _MeteredChat(_Proxy):
    @property
    def completions(self):
        return _MeteredCompletions(self._inner.completions, self._meter)


class _MeteredCompletions(_Proxy):
    def create(self, *args, **kwargs):
        try:
            resp = self._inner.create(*args, **kwargs)
        except Exception:
            self._meter.record_llm_failure()
            raise
        try:
            self._meter.record_llm(resp)
        except Exception:  # noqa: BLE001 - metering must not break a run
            pass
        return resp


class _MeteredTavily(_Proxy):
    def search(self, *args, **kwargs):
        try:
            resp = self._inner.search(*args, **kwargs)
        except Exception:
            self._meter.record_search_failure()
            raise
        try:
            self._meter.record_search(resp)
        except Exception:  # noqa: BLE001
            pass
        return resp
