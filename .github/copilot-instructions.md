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
  - Stores ATM IV data: `self.atm_iv_by_expiry[expirydt] = {'strike', 'call_iv', 'put_iv', 'avg_iv', 'call_price', 'put_price', 'call_oi', 'put_oi', 'upper_2sigma', 'lower_2sigma', 'two_sigma_move'}`
  - Calculates prev close: `self.prev_busday_close_price = lastSalePrice - netChange`
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
- Implied Volatility columns: `c_IV`, `p_IV` (decimal), `c_IV_%`, `p_IV_%` (percentage, rounded to 2 decimals)
- Database uses snake_case: `load_dt`, `tsla_spot_price`, `expirygroup`
- Expiry dates: stored as `expirygroup` in YYYY-MM-DD format, displayed as `'%b-%d-%Y'` (e.g., "Jan-10-2026")

### Date Handling
- Global date utilities: `next_friday`, `prev_monday`, `weekly_expiry_target` (6 weeks out)
- Always use `dateutil.parser.parse("Friday")` for relative dates
- Database timestamps: `load_dt` (YYYY-MM-DD) + `load_tm` (HH:MM:SS)
- Date format consistency: Use `strftime('%b-%d-%Y')` for abbreviated month (NOT `'%B-%d-%Y'`)

### Plotly Chart Configuration
- Multi-row subplots with `make_subplots(rows=N, cols=2, specs=[[{"secondary_y": True}]]*N)`
- Column 1: Open Interest + Volume (OI as filled area, Volume as bars on secondary_y)
- Column 2: Option Prices + IV curves (prices as lines, IV on secondary_y with 0-200% range)
- Open Interest: filled area charts (`fill='tozeroy'`, `line_shape='spline'`)
- Volume: bar charts on secondary y-axis (green for calls, red for puts)
- Current price: black dashed vertical line (`line_dash='dash'`)
- Previous close: gray dashed vertical line
- 2-Sigma bounds: orange dashed vertical lines with text annotations
- Color scheme: Cyan (rgb(0,200,200)) for call IV, Orange (rgb(255,165,0)) for put IV, Purple for ATM annotations

### Implied Volatility Calculations
- Black-Scholes model via `scipy.optimize.brentq` (search range: 0.01 to 5.0)
- ATM IV = average of call and put IV at strike closest to `lastSalePrice`
- ATM IV stored in `self.atm_iv_by_expiry[expirydt]['avg_iv']` for each expiry
- 2-Sigma calculation: `Stock Price × (IV/100) × √(days_to_expiry/365) × 2`
- IV displayed on secondary y-axis (0-200% range) alongside option prices

### Error Handling
- API failures: Always catch and fall back to Yahoo Finance
- Missing data: Use `df.fillna(0.001)` to avoid division by zero in ratios
- SQLite: All DB methods use `try/except` with `traceback.print_exc()`
- IV calculation failures: Silently continue (returns None), check `.notna().any()` before plotting

## Testing & Debugging
- No formal test suite; manual testing via browser
- Check terminal output for:
  - Proxy status: "Proxy enabled" vs "Proxy disabled"
  - Data saves: "Saved data to file" (every 15min when market open)
  - ATM IV console output: Displays strike, call IV, put IV, average IV (★ symbol), and 2σ range
  - Client counter: Cookie-based request tracking
- Database inspection: Use `db.query_sql_data()` for quick queries
- Debugpy: Configured to listen on port 5678 with `debugpy.wait_for_client()` (may need to remove in production)

## Common Pitfalls
- **Stale Data**: Nasdaq API caches aggressively; randomize User-Agent in `get_headers()`
- **Callback Loops**: Use `prevent_initial_call=True` and check `ctx.triggered[0]['prop_id']`
- **Volume Calculation**: Daily volume = current - start_of_day snapshot (see `show_volume` logic)
- **IV Percentage Units**: Always use decimal (0.4575) in calculations, convert to percentage (45.75%) only for display
- **Chart Overlaps**: Text annotations positioned at specific offsets (e.g., `lastSalePrice + 2.5` for x, `atm_iv_avg + 5` for y)
- **Date Format**: Use `'%b-%d-%Y'` consistently, NOT `'%B-%d-%Y'` (breaks in 5+ places if inconsistent)
- **JPM Automation**: `jpm_login.py` requires Selenium + Firefox; expects `JPM_USER`/`JPM_PASSWORD` env vars

## Statistical Analysis Features
- **ATM IV**: Industry standard = simple average of call and put IV at ATM strike
- **2-Sigma Bounds**: 95% confidence interval for price movement by expiration
- **Formula**: `Expected Move = Stock Price × IV × √(Days/365) × σ_multiplier`
- **Access data**: `tickr.atm_iv_by_expiry[expirydt]` contains all calculated metrics
- **IV Smile/Skew**: Visualized via cyan (call) and orange (put) IV curves on secondary y-axis

## Key Files
- [src/option_tracker/\_\_main\_\_.py](src/option_tracker/__main__.py): App entry point, navbar generation (runs on port 8050)
- [src/option_tracker/utils/pc_utils.py](src/option_tracker/utils/pc_utils.py): All data logic including Black-Scholes IV, 2σ calculations
- [src/option_tracker/pages/oic_downloader.py](src/option_tracker/pages/oic_downloader.py): Main dashboard with PUT/CALL charts
- [src/option_tracker/pages/option_chain.py](src/option_tracker/pages/option_chain.py): LEAP options view
- [src/option_tracker/pages/mmtm_trend.py](src/option_tracker/pages/mmtm_trend.py): Momentum trend analysis
- [pyproject.toml](pyproject.toml): Dependencies, requires Python >=3.11
