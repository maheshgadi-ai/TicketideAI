import json
import logging

from ticketideai.observability.logging_setup import JsonFormatter, configure_logging


def test_json_formatter_includes_extra_fields() -> None:
    record = logging.LogRecord("ticketideai.x", logging.INFO, __file__, 1, "done", None, None)
    record.tokens = 42
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "done"
    assert payload["tokens"] == 42
    assert payload["level"] == "INFO"


def test_configure_logging_sets_level_and_handler() -> None:
    configure_logging("debug", json_output=False)
    logger = logging.getLogger("ticketideai")
    assert logger.level == logging.DEBUG
    assert len(logger.handlers) == 1
    assert not isinstance(logger.handlers[0].formatter, JsonFormatter)
