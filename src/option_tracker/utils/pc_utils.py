import os
import re
import sys
import math
import time
import random
import socket
import hashlib
import pathlib
import sqlite3
import traceback
from enum import Enum, auto
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import dateutil.parser as dparse

# === Third-Party Imports ===
import requests
import numpy as np
import pandas as pd
import simplejson as json

_SQRT2 = math.sqrt(2.0)


def _norm_cdf(x):
    """Standard-normal CDF (replaces scipy.stats.norm.cdf; accurate to ~1e-16)."""
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _brentq(f, xa, xb, xtol=1e-6, maxiter=100):
    """Brent-Dekker root finder on a sign-bracketed interval (replaces scipy.optimize.brentq)."""
    a, b = xa, xb
    fa, fb = f(a), f(b)
    if fa == 0.0:
        return a
    if fb == 0.0:
        return b
    if fa * fb > 0.0:
        raise ValueError("f(a) and f(b) must have different signs")
    if abs(fa) < abs(fb):
        a, b, fa, fb = b, a, fb, fa
    c, fc = a, fa
    d = a
    mflag = True
    for _ in range(maxiter):
        if fb == 0.0 or abs(b - a) < xtol:
            return b
        if fa != fc and fb != fc:
            s = (a * fb * fc / ((fa - fb) * (fa - fc))
                 + b * fa * fc / ((fb - fa) * (fb - fc))
                 + c * fa * fb / ((fc - fa) * (fc - fb)))
        else:
            s = b - fb * (b - a) / (fb - fa)
        lo, hi = ((3.0 * a + b) / 4.0, b) if a < b else (b, (3.0 * a + b) / 4.0)
        if (not (lo < s < hi)
                or (mflag and abs(s - b) >= abs(b - c) / 2.0)
                or (not mflag and abs(s - b) >= abs(c - d) / 2.0)
                or (mflag and abs(b - c) < xtol)
                or (not mflag and abs(c - d) < xtol)):
            s = (a + b) / 2.0
            mflag = True
        else:
            mflag = False
        fs = f(s)
        d, c, fc = c, b, fb
        if fa * fs < 0.0:
            b, fb = s, fs
        else:
            a, fa = s, fs
        if abs(fa) < abs(fb):
            a, b, fa, fb = b, a, fb, fa
    return b


# === Database Error Import ===
from sqlite3 import Error
# import debugpy
# debugpy.listen(5678)
# debugpy.wait_for_client()
# ---------------------------
# Utility Functions
# ---------------------------

def get_host_ip():
    """Get the primary non-loopback IPv4 address of the host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # Doesn't actually connect to internet
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def configure_proxy():
    """Enable or disable proxy based on corporate network detection and alpacaproxy.
    
    Only runs if host IP starts with 10.122 (JPM corporate network).
    Alpacaproxy is auto-started at boot by corporate on localhost:9443.
    """
    host_ip = get_host_ip()
    print(f"Detected Host IP: {host_ip}")

    if not host_ip.startswith("10.122"):
        print("Proxy skipped (not on 10.122.x.x network).")
        return

    proxy_port = 9443

    # Check if alpacaproxy is running (auto-started at boot by corporate)
    proxy_running = False
    try:
        with socket.create_connection(("127.0.0.1", proxy_port), timeout=1):
            proxy_running = True
    except Exception:
        pass

    if proxy_running:
        os.environ["http_proxy"] = f"http://127.0.0.1:{proxy_port}"
        os.environ["https_proxy"] = f"http://127.0.0.1:{proxy_port}"
        print(f"Proxy enabled: alpacaproxy @ 127.0.0.1:{proxy_port}")
    else:
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        print("Proxy disabled (alpacaproxy not responding on localhost:9443).")


# ---------------------------
# Initialization
# ---------------------------

configure_proxy()

# Enum example
class OIC_State(Enum):
    IDLE = auto()
    RUNNING = auto()

# Date Calculations
next_friday = dparse.parse("Friday")
next_monday = dparse.parse("Monday")
one_week = timedelta(days=7)

weekly_expiry_target = next_friday + one_week * 6
run_dt_yyyy_mm_dd = datetime.today().strftime("%Y-%m-%d")

prev_friday = next_friday - one_week
prev_monday = next_monday - one_week

prev_friday_yyyy_mm_dd = prev_friday.strftime("%Y-%m-%d")

IV_CACHE_THRESHOLD = 0.50  # Dollar threshold for IV cache invalidation


def get_headers():
    return {'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.'+ str(random.random())+' Safari/537.36'}


def et_now():
    """Current ET wall-clock as a naive datetime, independent of the host timezone."""
    return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)


# --- Risk-free rate from the US Treasury par yield curve ---
_RATE_FALLBACK = 0.05  # used if the curve can't be fetched
_TENOR_DAYS = {
    "1 Mo": 30, "1.5 Month": 45, "2 Mo": 60, "3 Mo": 91, "4 Mo": 121,
    "6 Mo": 182, "1 Yr": 365, "2 Yr": 730, "3 Yr": 1095, "5 Yr": 1825,
    "7 Yr": 2555, "10 Yr": 3650, "20 Yr": 7300, "30 Yr": 10950,
}
_rate_curve = {"fetched": 0.0, "points": None, "attempted": 0.0}
_RATE_TTL = 12 * 3600      # refresh the curve at most every 12 hours
_RATE_RETRY = 300         # but retry every 5 min while a refresh is failing


def _fetch_treasury_curve():
    """Return [(days, rate_decimal), ...] from the latest Treasury par yield curve."""
    from io import StringIO
    year = datetime.now(ZoneInfo('America/New_York')).year
    url = (f'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/'
           f'daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve'
           f'&field_tdr_date_value={year}&page&_format=csv')
    resp = requests.get(url, headers=get_headers(), timeout=10)
    resp.raise_for_status()
    df = pd.read_csv(StringIO(resp.text))
    df['Date'] = pd.to_datetime(df['Date'])
    latest = df.sort_values('Date').iloc[-1]
    points = [(days, float(latest[col]) / 100.0)
              for col, days in _TENOR_DAYS.items()
              if col in latest.index and pd.notna(latest[col])]
    points.sort()
    return points


def get_risk_free_rate(days_to_expiry):
    """Annualized risk-free rate interpolated from the Treasury curve by tenor.

    Refreshed every 12h (retries every 5 min on failure); falls back to 5%.
    """
    now = time.time()
    stale = (now - _rate_curve["fetched"]) > _RATE_TTL
    if stale and (now - _rate_curve["attempted"]) > _RATE_RETRY:
        _rate_curve["attempted"] = now
        try:
            pts = _fetch_treasury_curve()
            if pts:
                _rate_curve["points"] = pts
                _rate_curve["fetched"] = time.time()
                print(f"Treasury curve loaded ({len(pts)} tenors, "
                      f"1Mo={pts[0][1]*100:.2f}%)")
        except Exception as e:
            print(f"Treasury curve fetch failed ({e}); "
                  f"using {'stale curve' if _rate_curve['points'] else '5% fallback'}")
    pts = _rate_curve["points"]
    if not pts:
        return _RATE_FALLBACK
    xs = [d for d, _ in pts]
    ys = [r for _, r in pts]
    return float(np.interp(max(days_to_expiry, 1), xs, ys))

def isNowInTimePeriod(startTime, endTime, nowTime):
    if startTime < endTime:
        return nowTime >= startTime and nowTime <= endTime
    else:
        #Over midnight:
        return nowTime >= startTime or nowTime <= endTime
class DB():
    def __init__(self,db_file):
        self.db_file = db_file
        PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
        self.DB_PATH = os.path.join(PROJECT_ROOT, self.db_file)
        # Ensure data directory exists
        os.makedirs(os.path.dirname(self.DB_PATH), exist_ok=True)

    def create_connection(self):
        conn = None
        try:
            conn = sqlite3.connect(self.DB_PATH)
            # Create tables if they don't exist
            self._create_tables(conn)
        except Error as e:
            print(f"Error connecting to database: {e}")
            print(traceback.print_exc())
        return conn

    def _create_tables(self, conn):
        """Create necessary tables if they don't exist"""
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tsla_nasdaq (
                    load_dt TEXT,
                    load_tm TEXT,
                    expiryDate TEXT,
                    strike REAL,
                    c_Last REAL,
                    p_Last REAL,
                    c_Change REAL,
                    p_Change REAL,
                    c_Volume INTEGER,
                    p_Volume INTEGER,
                    c_Openinterest INTEGER,
                    p_Openinterest INTEGER,
                    tsla_spot_price REAL,
                    PRIMARY KEY (load_dt, load_tm, expiryDate, strike)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS mmtm_daily (
                    load_dt TEXT PRIMARY KEY,
                    data TEXT
                )
            ''')
            conn.commit()
        except Error as e:
            print(f"Error creating tables: {e}")
            print(traceback.print_exc())

    def store_data(self,p_df, p_load_dt):
        # import pdb; pdb.set_trace()

        try:
            conn = self.create_connection()
            p_df['load_dt'] = p_load_dt
            insert_qry = ' insert or ignore into tsla_nasdaq (' + ','.join(p_df.columns) + ') values ('+str('?,'*len(p_df.columns))[:-1] +') '
            conn.executemany(insert_qry, p_df.to_records(index=False))
            conn.commit()
        except:
            e = sys.exc_info()[1]
            print(traceback.print_exc())

    def store_momentum_data(self,p_df, p_load_dt):
        # import pdb; pdb.set_trace()

        try:
            conn = self.create_connection()
            p_df['load_dt'] = p_load_dt
            insert_qry = ' insert or ignore into mmtm_daily (' + ','.join(p_df.columns) + ') values ('+str('?,'*len(p_df.columns))[:-1] +') '
            insert_qry = ' insert or ignore into mmtm_daily ("' + '","'.join(p_df.columns) + '") values ('+str('?,'*len(p_df.columns))[:-1] +') '
            conn.executemany(insert_qry, p_df.to_records(index=False))
            conn.commit()
        except:
            e = sys.exc_info()[1]
            print(traceback.print_exc())

    def query_spot_price(self,p_load_dt):
        sql_str = f'''select tsla_spot_price from tsla_nasdaq where load_dt = '{p_load_dt}' order by load_dt desc , load_tm desc limit 1 '''
        conn=self.create_connection()
        cur = conn.cursor()
        cur.execute(sql_str)
        ret_rows = cur.fetchall()
        ret_df = pd.DataFrame.from_dict(ret_rows)
        ret_df.columns = [description[0] for description in cur.description]
        return ret_df

    def query_data(self,p_load_dt):
        # get last row from previous working day
        sql_str = f''' select *
        from tsla_nasdaq where
        (load_dt,load_tm) = (select load_dt , load_tm from tsla_nasdaq where load_dt = '{p_load_dt}' order by load_dt desc , load_tm desc limit 1) '''
        conn=self.create_connection()
        cur = conn.cursor()
        cur.execute(sql_str)
        ret_rows = cur.fetchall()
        ret_df = pd.DataFrame.from_dict(ret_rows)
        ret_df.columns = [description[0] for description in cur.description]
        return ret_df

    def query_sql_data(self,p_sql):
        sql_str = p_sql
        conn=self.create_connection()
        cur = conn.cursor()
        cur.execute(sql_str)
        ret_rows = cur.fetchall()
        ret_df = pd.DataFrame.from_dict(ret_rows)
        ret_df.columns = [description[0] for description in cur.description]
        return ret_df

    def query_range_data(self,p_expiry, p_load_dt_start, p_load_dt_end):
        # get last row from previous working day
        sql_str = f''' select *
        from tsla_nasdaq where expiryDate = '{p_expiry}' and
        load_dt >= '{p_load_dt_start}' and load_dt <='{p_load_dt_end}' 
        order by load_dt desc , load_tm desc '''
        conn=self.create_connection()
        cur = conn.cursor()
        cur.execute(sql_str)
        ret_rows = cur.fetchall()
        ret_df = pd.DataFrame.from_dict(ret_rows)
        ret_df.columns = [description[0] for description in cur.description]
        return ret_df


PATH = pathlib.Path(__file__).parent
DATA_PATH = PATH.joinpath("../data").resolve()
db=DB(db_file=DATA_PATH.joinpath('data_store.sqlite'))


class Ticker():
    def __init__(self,ticker):
        self.ticker = ticker
        self.lastSalePrice = self.get_lastSalePrice()
        self.marketStatus = None
        self.lastDataStoreTime = None
        self.dataSource = None
        self.df_predicted_price = pd.DataFrame()
        # self.prevBusDay=self.get_prevBusDay()
        self.target_close = None
        self.target_close_lst = [self.target_close]
        self.df, self.fig = None, None
        self.dict_target={}
        self.state = OIC_State.IDLE
        self.atm_iv_by_expiry = {}  # Store ATM IV for each expiry date
        self.prev_busday_close_price = None  # Previous business day closing price
        self._iv_cache = None  # Cached IV DataFrames to skip recomputation
        self._dynamic_elements = None  # Index of dynamic figure elements for Patch updates
        self._prev_iv_guesses = {}  # Previous IV values for warm-start solver

    def set_state(self, state:OIC_State):
        self.state = state

    def _compute_strike_hash(self, df):
        """MD5 hash of expiry and strike sets to detect structural changes."""
        data = df[['expirygroup', 'strike']].astype(str).apply(tuple, axis=1).sort_values().values
        return hashlib.md5(str(data.tolist()).encode()).hexdigest()

    def _get_et_date(self):
        """Current date string (YYYY-MM-DD) in America/New_York timezone."""
        return datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')

    def get_lastSalePrice(self): #Realtime price
        url = f'https://api.nasdaq.com/api/quote/{self.ticker}/info?assetclass=stocks'
        # url = 'https://api.nasdaq.com/api/quote/TSLA/realtime-trades?&limit=10&fromTime=00:00'
        response = requests.get(url, headers=get_headers())
        lastSalePrice = response.json()['data']['primaryData']['lastSalePrice']
        netChange = response.json()['data']['primaryData']['netChange']
        self.marketStatus = response.json()['data']['marketStatus']

        self.lastSalePrice = float(re.findall(r"\d+\.\d+", lastSalePrice)[0])
        
        # Calculate previous business day closing price: Last Sale Price - Net Change
        try:
            netChange_float = float(netChange.replace(',', ''))
            self.prev_busday_close_price = self.lastSalePrice - netChange_float
        except (ValueError, AttributeError):
            self.prev_busday_close_price = None
        
        return lastSalePrice

    def get_prevBusDay(self):
        try:
            url = f'https://api.nasdaq.com/api/quote/{self.ticker}/historical?assetclass=stocks&fromdate={prev_friday_yyyy_mm_dd}&limit=1&todate={run_dt_yyyy_mm_dd}'
            response = requests.get(url, headers=get_headers())
            lastBusDay = response.json()['data']['tradesTable']['rows'][0]['date']
            self.lastBusDay_yyyy_mm_dd = pd.to_datetime(lastBusDay).strftime('%Y-%m-%d')
        except Exception as e:
            url = f'https://api.nasdaq.com/api/quote/{self.ticker}/info?assetclass=stocks'
            response = requests.get(url, headers=get_headers())
            self.lastBusDay_yyyy_mm_dd = pd.to_datetime(response.json()['data']['secondaryData']['lastTradeTimestamp'].split('ON')[1]).strftime(
                '%Y-%m-%d')

    def calculate_implied_volatility(self, option_price, stock_price, strike, time_to_expiry, risk_free_rate=0.05, option_type='call', initial_guess=None):
        """
        Calculate implied volatility using Black-Scholes model via Brent's method.
        
        Args:
            option_price: Current market price of the option
            stock_price: Current stock price
            strike: Strike price
            time_to_expiry: Time to expiration in years (days/365)
            risk_free_rate: Risk-free rate (default 5%)
            option_type: 'call' or 'put'
        
        Returns:
            float: Implied volatility (annualized), or None if calculation fails
        """
        
        def black_scholes_price(volatility):
            """Black-Scholes pricing formula"""
            if volatility <= 0 or time_to_expiry <= 0:
                return 0
            
            d1 = (np.log(stock_price / strike) + (risk_free_rate + 0.5 * volatility**2) * time_to_expiry) / (volatility * np.sqrt(time_to_expiry))
            d2 = d1 - volatility * np.sqrt(time_to_expiry)
            
            if option_type == 'call':
                price = stock_price * _norm_cdf(d1) - strike * np.exp(-risk_free_rate * time_to_expiry) * _norm_cdf(d2)
            else:  # put
                price = strike * np.exp(-risk_free_rate * time_to_expiry) * _norm_cdf(-d2) - stock_price * _norm_cdf(-d1)
            
            return price
        
        def objective_function(volatility):
            """Function to minimize: difference between market and model price"""
            return black_scholes_price(volatility) - option_price
        
        try:
            # Warm-start: try narrowed bracket first if initial_guess is available
            if initial_guess is not None and 0.01 < initial_guess < 5.0:
                lo = max(0.01, initial_guess * 0.5)
                hi = min(5.0, initial_guess * 2.0)
                try:
                    return _brentq(objective_function, lo, hi, xtol=1e-6, maxiter=100)
                except (ValueError, RuntimeError):
                    pass  # Fall through to full range
            # Full range search
            implied_vol = _brentq(objective_function, 0.01, 5.0, xtol=1e-6, maxiter=100)
            return implied_vol
        except (ValueError, RuntimeError):
            # Failed to converge or invalid inputs
            return None

    def add_implied_volatility_columns(self, expirydt, df_expiry):
        """
        Add current IV columns to dataframe for calls and puts.
        
        Calculates implied volatility based on current option prices using Black-Scholes.
        Shows IV smile/skew across strikes.
        
        Args:
            df_expiry: DataFrame with columns: strike, expirygroup, c_Last, p_Last
        
        Returns:
            DataFrame with added columns: c_IV, p_IV, c_IV_%, p_IV_%
        """
        
        # Make a copy to avoid SettingWithCopyWarning
        df_expiry = df_expiry.copy()
        
        # Get expiry date and calculate time to expiry
        # expirydt can be a string, datetime, or Timestamp
        if isinstance(expirydt, str):
            expiry_date = pd.to_datetime(expirydt, format='%b-%d-%Y')
        else:
            expiry_date = pd.to_datetime(expirydt)
        
        days_to_expiry = (expiry_date - datetime.today()).days
        # Trading-day tenor (business days / 252) to match brokerage IV quoting;
        # calendar days only for the risk-free-rate tenor lookup.
        expiry_close = expiry_date.replace(hour=16, minute=0, second=0, microsecond=0)
        _now = et_now()
        cal_days = max((expiry_close - _now).total_seconds() / 86400.0, 0.0)
        bus_days = max(float(np.busday_count(_now.date(), expiry_close.date())), 0.25)
        time_to_expiry = max(bus_days / 252.0, 1e-5)
        risk_free_rate = get_risk_free_rate(cal_days)
        
        # Get current stock price
        stock_price = self.lastSalePrice
        
        # Initialize IV columns with None
        df_expiry['c_IV'] = None
        df_expiry['p_IV'] = None

        # Use bid/ask mid ("mark") for IV when available; fall back to Last (stale-print safe).
        for _side in ('c', 'p'):
            _last = pd.to_numeric(df_expiry[f'{_side}_Last'], errors='coerce')
            _bid_c, _ask_c = f'{_side}_Bid', f'{_side}_Ask'
            if _bid_c in df_expiry.columns and _ask_c in df_expiry.columns:
                _bid = pd.to_numeric(df_expiry[_bid_c], errors='coerce')
                _ask = pd.to_numeric(df_expiry[_ask_c], errors='coerce')
                _mid = (_bid + _ask) / 2
                _use = _bid.notna() & _ask.notna() & (_bid > 0) & (_ask > 0)
                df_expiry[f'{_side}_mark'] = _mid.where(_use, _last)
            else:
                df_expiry[f'{_side}_mark'] = _last
        
        # Calculate IV for each row
        for idx in df_expiry.index:
            try:
                strike = df_expiry.loc[idx, 'strike']
                
                # Look up previous IV for warm-start
                expiry_key = expirydt if isinstance(expirydt, str) else pd.to_datetime(expirydt).strftime('%b-%d-%Y')
                c_guess = self._prev_iv_guesses.get((expiry_key, strike, 'call'))
                p_guess = self._prev_iv_guesses.get((expiry_key, strike, 'put'))
                
                # Calculate current call IV
                if df_expiry.loc[idx, 'c_mark'] > 0.02:
                    c_iv = self.calculate_implied_volatility(
                        df_expiry.loc[idx, 'c_mark'], 
                        stock_price, 
                        strike, 
                        time_to_expiry, 
                        risk_free_rate=risk_free_rate,
                        option_type='call',
                        initial_guess=c_guess
                    )
                    df_expiry.loc[idx, 'c_IV'] = c_iv
                    if c_iv is not None:
                        self._prev_iv_guesses[(expiry_key, strike, 'call')] = c_iv
                
                # Calculate current put IV
                if df_expiry.loc[idx, 'p_mark'] > 0.02:
                    p_iv = self.calculate_implied_volatility(
                        df_expiry.loc[idx, 'p_mark'], 
                        stock_price, 
                        strike, 
                        time_to_expiry, 
                        risk_free_rate=risk_free_rate,
                        option_type='put',
                        initial_guess=p_guess
                    )
                    df_expiry.loc[idx, 'p_IV'] = p_iv
                    if p_iv is not None:
                        self._prev_iv_guesses[(expiry_key, strike, 'put')] = p_iv
            
            except Exception as e:
                # Skip this row if calculation fails
                continue
        
        # Convert to percentage for display (rounded to 2 decimals)
        df_expiry['c_IV_%'] = (df_expiry['c_IV'] * 100).astype(float).round(2)
        df_expiry['p_IV_%'] = (df_expiry['p_IV'] * 100).astype(float).round(2)
        
        return df_expiry


# prev_bus_day_closing price: https://api.nasdaq.com/api/quote/TSLA/historical?assetclass=stocks&fromdate=2021-06-06&limit=1&todate=2021-07-06

    def oic_api_call(self):
        load_dt = datetime.today().strftime('%Y-%m-%d')
        weekly_expiry_end = weekly_expiry_target.strftime('%Y-%m-%d')

        url = f'https://api.nasdaq.com/api/quote/{self.ticker}/option-chain?assetclass=stocks&limit=100&fromdate={load_dt}&todate={weekly_expiry_end}&excode=oprac&callput=callput&money=at&type=all'
        response = requests.get(url, headers=get_headers())
        # Nasdaq returns data.table = null after hours / when throttled; guard the
        # whole path so a missing table degrades to "no data" instead of crashing.
        payload = response.json() if response.content else None
        data = payload.get('data') if isinstance(payload, dict) else None
        table = data.get('table') if isinstance(data, dict) else None
        rows = table.get('rows') if isinstance(table, dict) else None
        if rows:
            df = pd.DataFrame.from_dict(rows)
            self.dataSource = 'Nasdaq'
        else:
            print('No data returned from Nasdaq API')
            return pd.DataFrame()


        df['expirygroup'] = df['expirygroup'].apply(lambda x: pd.to_datetime(x))
        df['expirygroup']=df['expirygroup'].ffill(axis=0)
        df.dropna(inplace=True)
        df['load_dt'] = datetime.today().strftime('%Y-%m-%d')
        df['load_tm'] = datetime.today().strftime('%H:%M:%S')
        if self.marketStatus =='Market Open':  # Save data only during market hours
            try:
                if (datetime.today()-self.lastDataStoreTime).seconds/60 > 15:
                    df.drop(['expirygroup','c_colour','p_colour','drillDownURL'],axis=1,inplace=True)
                    df['tsla_spot_price'] = self.lastSalePrice
                    db.store_data(p_df=df, p_load_dt=load_dt)
                    print('Saved data to file')
                    self.lastDataStoreTime = datetime.today()
            except :
                self.lastDataStoreTime = datetime.today()


        return df


def convert_dt_to_str(p_dt_list):
    return [f"{pd.to_datetime(dt).strftime('%b %d %Y')}" for dt in p_dt_list]

class Nasdaq_Leap():
    def __int__(self):
        self.df, self.dict_color = None, None

    def get_nasdaq_leap_option_chain(self):
        url = 'https://api.nasdaq.com/api/quote/TSLA/option-chain?assetclass=stocks&limit=6000&fromdate=all&todate=all&excode=oprac&callput=call&money=out&type=all'
        res = requests.get(url, headers=get_headers())
        df = pd.DataFrame(json.loads(res.text)['data']['table']['rows'])

        df['expirygroup']=df['expirygroup'].replace('',np.nan)
        df['expirygroup']=df['expirygroup'].ffill()
        df['expirygroup']=pd.to_datetime(df.expirygroup)
        df['expirygroup']=convert_dt_to_str(df.expirygroup.values)

        df['drillDownURL']=df['drillDownURL'].apply(
            lambda x: f'https://app.quotemedia.com/quotetools/getChart?webmasterId=90423&symbol=@{x[59:]}&chscale=6m&chwid=700&chhig=300' 
            if isinstance(x, str) and len(x) > 59 
            else ''
)
        df.drillDownURL = df.drillDownURL.str.replace('--','  ').values


        num_of_expirydts=len(sorted(df.expirygroup.unique()))
        char_url = "https://app.quotemedia.com/quotetools/getChart?webmasterId=90423&symbol=@TSLA%20%20220916C01800000&chscale=6m&chwid=1000&chhig=300"

        def get_evenly_divided_values(value_to_be_distributed, times):
            return [value_to_be_distributed // times + int(x < value_to_be_distributed % times) for x in range(times)]
        green_range = get_evenly_divided_values(255,num_of_expirydts)

        dict_color=dict(zip(df.expirygroup.unique(), reversed(np.cumsum(green_range))))
        df['color']=df.expirygroup.map(dict_color)

        df[df.filter(regex='c_|p_|strike').columns] = df.filter(regex='c_|p_|strike').\
            apply(pd.to_numeric,errors='coerce')
        df=df[df.strike>160].copy()
        print (f'{get_evenly_divided_values.__name__} : finished Data Manipulation')
        self.df, self.dict_color = df, dict_color
        return self.df, self.dict_color
