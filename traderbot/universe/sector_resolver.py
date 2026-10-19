"""Dynamic sector ETF and benchmark resolver for TraderBot symbols.

Eliminates manual maintenance of 80-line benchmark_by_symbol mappings by
automatically resolving standard US stocks to their SPDR Sector ETF.
"""

from typing import Any, Dict, Optional

# Standard SPDR Sector ETF mappings for standard US industry sectors
SECTOR_NAME_TO_ETF: Dict[str, str] = {
    "TECHNOLOGY": "XLK",
    "INFORMATION TECHNOLOGY": "XLK",
    "SEMICONDUCTORS": "XLK",
    "SOFTWARE": "XLK",
    "INDUSTRIALS": "XLI",
    "AEROSPACE": "XLI",
    "DEFENSE": "XLI",
    "HEALTHCARE": "XLV",
    "HEALTH CARE": "XLV",
    "BIOTECH": "XLV",
    "PHARMACEUTICALS": "XLV",
    "COMMUNICATION SERVICES": "XLC",
    "MEDIA": "XLC",
    "ENTERTAINMENT": "XLC",
    "CONSUMER DISCRETIONARY": "XLY",
    "RETAIL": "XLY",
    "AUTOMOBILES": "XLY",
    "CONSUMER STAPLES": "XLP",
    "FOOD": "XLP",
    "BEVERAGE": "XLP",
    "ENERGY": "XLE",
    "OIL": "XLE",
    "GAS": "XLE",
    "FINANCIALS": "XLF",
    "BANKS": "XLF",
    "INSURANCE": "XLF",
    "MATERIALS": "XLB",
    "CHEMICALS": "XLB",
    "MINING": "XLB",
    "UTILITIES": "XLU",
    "POWER": "XLU",
    "REAL ESTATE": "XLRE",
    "REIT": "XLRE",
    "CLEAN TECH": "ICLN",
    "RENEWABLE ENERGY": "ICLN",
}

ETF_TO_SECTOR_NAME: Dict[str, str] = {
    "XLK": "Information Technology",
    "XLI": "Industrials",
    "XLV": "Health Care",
    "XLC": "Communication Services",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLB": "Materials",
    "XLU": "Utilities",
    "XLRE": "Real Estate",
    "ICLN": "Clean Energy",
    "QQQ": "Broad Tech / Nasdaq",
    "SPY": "Broad Market / S&P 500",
}

# Known symbol to sector ETF dictionary (including all TraderBot universe symbols + major US market leaders)
SYMBOL_TO_SECTOR_ETF: Dict[str, str] = {
    # --- TraderBot Universe Incumbents ---
    "AIRJ": "XLI",
    "ALGS": "XLK",
    "ALMU": "XLI",
    "AMKR": "XLK",
    "AMPX": "ICLN",
    "ANET": "XLK",
    "APLD": "QQQ",
    "ARM": "XLK",
    "ASML": "XLK",
    "ASTS": "XLC",
    "AZN": "XLV",
    "BE": "ICLN",
    "BGR": "XLE",
    "BLDP": "ICLN",
    "BWMX": "XLY",
    "CEG": "XLU",
    "CIBR": "XLK",
    "COHR": "XLK",
    "CRDO": "XLK",
    "CRM": "XLK",
    "CRS": "XLI",
    "CRWD": "XLK",
    "CRWV": "QQQ",
    "DDOG": "XLK",
    "DPRO": "XLI",
    "EMO": "XLE",
    "ETN": "XLI",
    "ETO": "XLI",
    "FCEL": "ICLN",
    "FLNC": "ICLN",
    "GD": "XLI",
    "GEV": "XLI",
    "GFS": "XLK",
    "GOOG": "XLC",
    "GOOGL": "XLC",
    "GRPN": "XLY",
    "HSAI": "XLI",
    "HUBB": "XLI",
    "IBM": "XLK",
    "IDVO": "SPY",
    "IGV": "XLK",
    "INTC": "XLK",
    "IRDM": "XLC",
    "IREN": "QQQ",
    "IRWD": "XLV",
    "JOBY": "XLI",
    "KSLV": "XLB",
    "LMND": "XLF",
    "LOGI": "XLK",
    "LRCX": "XLK",
    "MDLN": "XLV",
    "MIRM": "XLV",
    "MRAM": "XLK",
    "MRVL": "XLK",
    "MSFT": "XLK",
    "MU": "XLK",
    "NFLX": "XLC",
    "ON": "XLK",
    "ORCL": "XLK",
    "P": "XLC",
    "PANW": "XLK",
    "PLTR": "XLK",
    "PLUG": "ICLN",
    "PODD": "XLV",
    "POWW": "XLI",
    "PUMP": "XLE",
    "RBBN": "XLK",
    "RDW": "XLI",
    "RKLB": "XLI",
    "SBUX": "XLY",
    "SMCI": "XLK",
    "SNOW": "XLK",
    "STX": "XLK",
    "TEM": "XLV",
    "TGLS": "XLI",
    "TM": "XLY",
    "TSEM": "XLK",
    "USAR": "XLB",
    "UUUU": "XLE",
    "VRT": "XLI",
    "WAT": "XLV",
    "WDC": "XLK",
    "ZIM": "XLI",
    "ZM": "XLK",

    # --- Common Mega-Cap & Popular S&P 500 / Nasdaq Leaders ---
    "NVDA": "XLK",
    "AAPL": "XLK",
    "AMD": "XLK",
    "AVGO": "XLK",
    "QCOM": "XLK",
    "TXN": "XLK",
    "AMAT": "XLK",
    "ADBE": "XLK",
    "NOW": "XLK",
    "INTU": "XLK",
    "AMZN": "XLY",
    "TSLA": "XLY",
    "META": "XLC",
    "DIS": "XLC",
    "CMCSA": "XLC",
    "TMUS": "XLC",
    "VZ": "XLC",
    "T": "XLC",
    "JPM": "XLF",
    "BAC": "XLF",
    "WFC": "XLF",
    "GS": "XLF",
    "MS": "XLF",
    "V": "XLF",
    "MA": "XLF",
    "BLK": "XLF",
    "C": "XLF",
    "LLY": "XLV",
    "UNH": "XLV",
    "JNJ": "XLV",
    "ABBV": "XLV",
    "MRK": "XLV",
    "PFE": "XLV",
    "TMO": "XLV",
    "ABT": "XLV",
    "ISRG": "XLV",
    "XOM": "XLE",
    "CVX": "XLE",
    "COP": "XLE",
    "SLB": "XLE",
    "EOG": "XLE",
    "GE": "XLI",
    "CAT": "XLI",
    "HON": "XLI",
    "UNP": "XLI",
    "BA": "XLI",
    "RTX": "XLI",
    "LMT": "XLI",
    "NOC": "XLI",
    "DE": "XLI",
    "UPS": "XLI",
    "FDX": "XLI",
    "WM": "XLI",
    "NEE": "XLU",
    "SO": "XLU",
    "DUK": "XLU",
    "AEP": "XLU",
    "SRE": "XLU",
    "LIN": "XLB",
    "SHW": "XLB",
    "APD": "XLB",
    "FCX": "XLB",
    "ECL": "XLB",
    "NEM": "XLB",
    "PLD": "XLRE",
    "AMT": "XLRE",
    "EQIX": "XLRE",
    "SPG": "XLRE",
    "PSA": "XLRE",
    "PG": "XLP",
    "COST": "XLP",
    "KO": "XLP",
    "PEP": "XLP",
    "WMT": "XLP",
    "PM": "XLP",
    "MDLZ": "XLP",
}


def resolve_sector_etf(symbol: str, supervisor_config: Optional[Dict[str, Any]] = None) -> str:
    """Resolve the appropriate sector ETF for a given symbol.
    
    Resolution order:
    1. Explicit override in supervisor_config['position_health']['benchmark_by_symbol']
    2. Explicit override in supervisor_config['benchmark_by_symbol']
    3. Built-in SYMBOL_TO_SECTOR_ETF dictionary
    4. supervisor_config['position_health']['benchmark_symbol']
    5. Fallback to broad market 'SPY' (never 'Unmapped')
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return "SPY"

    cfg = supervisor_config or {}
    health_cfg = cfg.get("position_health") or {}

    # 1. Position health benchmark_by_symbol
    override = (health_cfg.get("benchmark_by_symbol") or {}).get(sym)
    if override:
        return str(override).upper()

    # 2. Root benchmark_by_symbol (if any)
    root_override = (cfg.get("benchmark_by_symbol") or {}).get(sym)
    if root_override:
        return str(root_override).upper()

    # 3. Built-in sector map
    if sym in SYMBOL_TO_SECTOR_ETF:
        return SYMBOL_TO_SECTOR_ETF[sym]

    # 4. Configured benchmark symbol fallback
    benchmark = health_cfg.get("benchmark_symbol") or cfg.get("benchmark_symbol")
    if benchmark:
        return str(benchmark).upper()

    # 5. Default broad market
    return "SPY"


def sector_name_to_etf(sector_name: str, default: str = "SPY") -> str:
    """Map human sector name to corresponding SPDR ETF."""
    key = str(sector_name or "").upper().strip()
    return SECTOR_NAME_TO_ETF.get(key, default)


def etf_to_sector_name(etf: str) -> str:
    """Map ETF symbol to human sector name."""
    key = str(etf or "").upper().strip()
    return ETF_TO_SECTOR_NAME.get(key, "General Equities")
