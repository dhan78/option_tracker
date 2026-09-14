# Option Tracker

A Python-based dashboard for tracking and analyzing stock options using Dash.

## Requirements

- Python 3.11 or higher (system install — managed Pythons are blocked by JPM group policy)
- [`uv`](https://docs.astral.sh/uv/) installed

## One-time setup (JPM corporate environment)

Persist the two environment variables so `uv` / `uvx` use the system Python and the internal Artifactory index instead of the blocked managed Python and public PyPI:

```cmd
setx UV_PYTHON_PREFERENCE only-system
setx UV_INDEX_URL https://jetae-publish.prod.aws.jpmchase.net/artifactory/api/pypi/pypi/simple
```

> `setx` does NOT affect the current shell. Open a new terminal afterward so the variables are inherited.

Verify in a fresh terminal:

```cmd
echo %UV_PYTHON_PREFERENCE%
echo %UV_INDEX_URL%
```

## Installation & Usage

### Option A — Run directly with `uvx` (no manual venv)

```cmd
uvx --from I:\option-tracker option-tracker
```

Install from GitHub instead:

```bash
uvx --from I:\option-tracker option-tracker
```

### Option B — Editable dev install with `uv sync`

From the project root:

```cmd
uv sync
uv run option-tracker
```

Either option starts the Dash server. Open your browser at:

- http://localhost:8050

## Features

- Real-time option chain visualization
- Historical options data analysis
- Target price modeling
- Interactive charts and graphs
