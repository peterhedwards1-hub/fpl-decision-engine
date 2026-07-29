"""Launch the Streamlit manager interface."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    try:
        from streamlit.web import cli as streamlit_cli
    except ImportError as error:
        raise SystemExit(
            "The browser interface is not installed. Run: "
            "python -m pip install -e \".[ui]\""
        ) from error

    app_path = Path(__file__).with_name("app.py")
    sys.argv = ["streamlit", "run", str(app_path), *sys.argv[1:]]
    raise SystemExit(streamlit_cli.main())
