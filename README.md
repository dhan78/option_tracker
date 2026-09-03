# Option Tracker

A Python-based dashboard for tracking and analyzing stock options using Dash.

## Requirements

- Python 3.11 or higher
- [`uv`](https://docs.astral.sh/uv/) installed

## Installation & Usage

### Option A — Run directly with `uvx` (no manual venv)

```bash
uvx --from git+https://github.com/dhan78/option_tracker.git option-tracker-web
```

Install from GitHub instead:

```bash
uvx --from git+https://github.com/dhan78/option_tracker.git option-tracker-web
```

### Option B — Editable dev install with `uv sync`

From the project root:

```bash
cd /var/home/admin/IdeaProjects/option_tracker
uv sync
uv run option-tracker-web
```

Either option starts the web server. Open your browser at:

- http://localhost:8050

## Features

- Real-time option chain visualization
- Historical options data analysis
- Target price modeling
- Interactive charts and graphs
