"""Optional Langfuse tracing behind a small, failure-tolerant wrapper.

When Langfuse keys are absent (or the client fails), every method is a no-op, so
the rest of the app never needs to check whether tracing is on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from typing import Any, Literal

from ticketideai.config.settings import Settings

logger = logging.getLogger(__name__)

ObservationType = Literal["span", "generation", "agent", "tool", "chain", "evaluator"]

# Trace-level attributes on every trace, so this app's traces can be filtered when the
# Langfuse project is shared with other apps.
TRACE_TAGS = ["app:ticketideai"]
TRACE_METADATA = {"app": "ticketideai"}

Propagator = Callable[..., AbstractContextManager[Any]]


class Observation:
    """Handle for an in-progress trace observation. Safe to use when tracing is off."""

    def __init__(self, inner: Any | None = None) -> None:
        self._inner = inner

    def update(self, **fields: Any) -> None:
        """Attach fields (output, metadata, usage_details, cost_details, level...)."""
        if self._inner is None:
            return
        try:
            self._inner.update(**fields)
        except Exception:
            logger.debug("Langfuse update failed", exc_info=True)


class Tracer:
    """Thin wrapper over a Langfuse client; disabled when `client` is None.

    `propagate` is Langfuse's `propagate_attributes`. When given, every observation
    is opened inside it, so the whole trace carries `TRACE_TAGS` and `TRACE_METADATA`.
    """

    def __init__(self, client: Any | None = None, propagate: Propagator | None = None) -> None:
        self._client = client
        self._propagate = propagate

    @property
    def enabled(self) -> bool:
        """True when a Langfuse client is attached."""
        return self._client is not None

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        as_type: ObservationType = "span",
        input: Any = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> Iterator[Observation]:
        """Open a nested observation for the duration of the `with` block.

        Exceptions raised inside the block mark the observation as an error and re-raise.
        """
        if self._client is None:
            yield Observation()
            return

        kwargs: dict[str, Any] = {"name": name, "as_type": as_type, "input": input}
        if metadata:
            kwargs["metadata"] = metadata
        if model:
            kwargs["model"] = model
        with ExitStack() as stack:
            self._enter_trace_attributes(stack)
            try:
                ctx = self._client.start_as_current_observation(**kwargs)
                inner = ctx.__enter__()
            except Exception:
                logger.warning("Langfuse observation could not start; continuing", exc_info=True)
                yield Observation()
                return

            obs = Observation(inner)
            try:
                yield obs
            except BaseException as exc:
                obs.update(level="ERROR", status_message=str(exc))
                self._safe_exit(ctx, exc)
                raise
            else:
                self._safe_exit(ctx, None)

    def _enter_trace_attributes(self, stack: ExitStack) -> None:
        if self._propagate is None:
            return
        try:
            stack.enter_context(self._propagate(tags=TRACE_TAGS, metadata=TRACE_METADATA))
        except Exception:
            logger.debug("Langfuse trace attributes could not be set", exc_info=True)

    def current_trace_id(self) -> str | None:
        """Return the active trace ID, if any."""
        if self._client is None:
            return None
        try:
            return self._client.get_current_trace_id()
        except Exception:
            logger.debug("Could not read current trace id", exc_info=True)
            return None

    def score(
        self, name: str, value: float, *, trace_id: str | None = None, comment: str | None = None
    ) -> None:
        """Attach a numeric score to a trace (the current one if `trace_id` is None)."""
        if self._client is None:
            return
        try:
            if trace_id is None:
                self._client.score_current_trace(name=name, value=value, comment=comment)
            else:
                self._client.create_score(
                    name=name, value=value, trace_id=trace_id, comment=comment
                )
        except Exception:
            logger.warning("Langfuse score failed for %s", name, exc_info=True)

    def flush(self) -> None:
        """Send any buffered events."""
        if self._client is None:
            return
        try:
            self._client.flush()
        except Exception:
            logger.warning("Langfuse flush failed", exc_info=True)

    @staticmethod
    def _safe_exit(ctx: Any, exc: BaseException | None) -> None:
        try:
            if exc is None:
                ctx.__exit__(None, None, None)
            else:
                ctx.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            logger.debug("Langfuse observation close failed", exc_info=True)


def build_tracer(settings: Settings) -> Tracer:
    """Create a `Tracer` from settings; returns a disabled tracer if keys are missing."""
    if not settings.tracing_enabled:
        logger.info("Langfuse tracing disabled (keys not set)")
        return Tracer()
    try:
        from langfuse import Langfuse, is_langfuse_span, propagate_attributes

        assert settings.langfuse_public_key and settings.langfuse_secret_key
        # Provider SDKs emit their own spans for each call. `LLMProvider.complete` already
        # records usage and cost, so exporting those spans would count every call twice.
        client = Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
            should_export_span=is_langfuse_span,
        )
    except Exception:
        logger.warning("Langfuse client init failed; tracing disabled", exc_info=True)
        return Tracer()
    logger.info("Langfuse tracing enabled", extra={"base_url": settings.langfuse_base_url})
    return Tracer(client, propagate=propagate_attributes)
