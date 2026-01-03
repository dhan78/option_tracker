# Option Tracker - AI Agent Instructions

## Project Overview
Stock options analytics dashboard using Dash for real-time and historical options data visualization. Primary ticker: TSLA. Data sources: Nasdaq API (primary) with Yahoo Finance fallback.

## Architecture

### Core Components
- **Dash Multi-Page App**: Entry point in `src/option_tracker/__main__.py`, registers pages automatically from `pages/` directory
- **Data Layer**: SQLite database (`data/data_store.sqlite`) with 15-minute auto-save during market hours
- **API Integration**: 
  - Primary: Nasdaq API (requires randomized User-Agent headers via `get_headers()`)
  - Fallback: Yahoo Finance via `yfinance` with cached session (`get_yahoo_session()`)

### Key Classes in `utils/pc_utils.py`
- **`Ticker(ticker_symbol)`**: Main data fetcher, manages state (`OIC_State.IDLE|RUNNING`), fetches real-time prices and option chains
- **`DB(db_file)`**: SQLite wrapper with auto-table creation, stores option chain snapshots every 15min during market hours
- **`OptionChart(expiry_dt, strike)`**: Historical drill-down for specific option contracts
- **`Nasdaq_Leap()`**: LEAP options (long-dated calls) visualization with color-coded expiry dates
- **`YahooFinance(ticker, num_of_weeks)`**: Fallback data source when Nasdaq API fails

## Critical Patterns

### State Management
- `Ticker` class maintains state to prevent concurrent API calls: check `tickr.state` before operations
- Timer-based refresh: 30s for main charts (`interval-component`), 15m for LEAP options

### Data Flow
1. `Ticker.oic_api_call()` fetches from Nasdaq → falls back to Yahoo if no data
2. Auto-saves to SQLite only when `marketStatus == 'Market Open'` and >15min since last save
3. `get_charts()` accepts `replay=True` kwarg to render historical data from database

### Proxy Configuration
- `configure_proxy()` runs at module import, auto-detects JPM corporate network
- Sets `http_proxy`/`https_proxy` env vars if `approxy.jpmchase.net:8080` is reachable
- Required for Nasdaq API access in corporate environments

### Page Registration
Pages in `pages/` directory must:
- Call `dash.register_page(__name__, path='/', title='...', name='...')`
- Define a `layout` variable (can be function or Dash component)
- Use callback decorators: `@dash.callback()` or `@callback()` (both work)

## Development Workflows

### Running Locally
```bash
# Install in editable mode
uv pip install -e .

# Run the dashboard
option-tracker
# Or: python -m option_tracker

# Access at http://localhost:8050
```

### Database Queries
```python
from option_tracker.utils.pc_utils import db

# Get latest snapshot
df = db.query_data('2024-01-03')

# Historical range for specific expiry
df = db.query_range_data('2024-12-20', '2024-01-01', '2024-01-03')

# Custom SQL
df = db.query_sql_data("SELECT * FROM tsla_nasdaq WHERE load_dt = '2024-01-03'")
```

### Adding New Pages
1. Create `src/option_tracker/pages/new_page.py`
2. Register with `dash.register_page(__name__, path='/url', name='Nav Label')`
3. Define `layout` (component tree)
4. Add callbacks with `@dash.callback()` decorator
5. Page auto-appears in navbar (see `__main__.py` navbar construction)

## Project-Specific Conventions

### Naming
- DataFrame columns use `c_` prefix for calls, `p_` prefix for puts (e.g., `c_Last`, `p_Volume`)
- Database uses snake_case: `load_dt`, `tsla_spot_price`, `expirygroup`
- Expiry dates: stored as `expirygroup` in YYYY-MM-DD format, displayed as "Month-DD-YYYY"

### Date Handling
- Global date utilities: `next_friday`, `prev_monday`, `weekly_expiry_target` (6 weeks out)
- Always use `dateutil.parser.parse("Friday")` for relative dates
- Database timestamps: `load_dt` (YYYY-MM-DD) + `load_tm` (HH:MM:SS)

### Plotly Chart Configuration
- Multi-row subplots with `make_subplots(rows=N, cols=2, specs=[[{"secondary_y": True}]]*N)`
- Open Interest: filled area charts (`fill='tozeroy'`, `line_shape='spline'`)
- Volume: bar charts on secondary y-axis
- Current price: vertical dashed line + text annotation at `y_max * 0.9`

### Error Handling
- API failures: Always catch and fall back to Yahoo Finance
- Missing data: Use `df.fillna(0.001)` to avoid division by zero in ratios
- SQLite: All DB methods use `try/except` with `traceback.print_exc()`

## Testing & Debugging
- No formal test suite; manual testing via browser
- Check terminal output for:
  - Proxy status: "Proxy enabled" vs "Proxy disabled"
  - Data saves: "Saved data to file" (every 15min when market open)
  - Client counter: Cookie-based request tracking
- Database inspection: Use `db.query_sql_data()` for quick queries

## Common Pitfalls
- **Stale Data**: Nasdaq API caches aggressively; randomize User-Agent in `get_headers()`
- **Callback Loops**: Use `prevent_initial_call=True` and check `ctx.triggered[0]['prop_id']`
- **Volume Calculation**: Daily volume = current - start_of_day snapshot (see `show_volume` logic)
- **JPM Automation**: `jpm_login.py` requires Selenium + Firefox; expects `JPM_USER`/`JPM_PASSWORD` env vars

## Key Files
- [src/option_tracker/\_\_main\_\_.py](src/option_tracker/__main__.py): App entry point, navbar generation
- [src/option_tracker/utils/pc_utils.py](src/option_tracker/utils/pc_utils.py): All data logic (882 lines)
- [src/option_tracker/pages/oic_downloader.py](src/option_tracker/pages/oic_downloader.py): Main dashboard with PUT/CALL charts
- [src/option_tracker/pages/option_chain.py](src/option_tracker/pages/option_chain.py): LEAP options view
- [pyproject.toml](pyproject.toml): Dependencies, requires Python >=3.11
