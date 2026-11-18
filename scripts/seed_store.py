"""Generate the deterministic TicketideAI SQLite store database."""

from __future__ import annotations

import argparse
from pathlib import Path

from ticketideai.tools.seed_store import generate_store

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="data/ticketide.sqlite", type=Path)
    generate_store(parser.parse_args().path)
