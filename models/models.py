from typing import Literal, Optional
from pydantic import BaseModel, Field

# ---------- User choices ----------

Profile = Literal["aggressive", "balanced", "conservative"]
Horizon = Literal["short", "medium", "long"]


class UserPrefs(BaseModel):
    profile: Profile    # how much risk the user accepts
    horizon: Horizon    # how long they plan to hold


# ---------- Market data (what the tools will return) ----------
# All percentage fields hold plain percent numbers: 15.18 means 15.18%.
# Every metric is Optional, because Yahoo does not report everything for every stock.

class DailyBar(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class PriceData(BaseModel):
    ticker: str
    latest_close: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    volatility_pct: Optional[float] = None          # annualized
    max_drawdown_pct: Optional[float] = None        # negative number, e.g. -22.5
    current_drawdown_pct: Optional[float] = None    # negative number, 0 = at peak
    avg_volume: Optional[int] = None
    recent_bars: list[DailyBar] = Field(default_factory=list)


class FundamentalsData(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    sector: Optional[str] = None
    market_cap: Optional[float] = None
    beta: Optional[float] = None
    trailing_pe: Optional[float] = None
    forward_pe: Optional[float] = None
    price_to_book: Optional[float] = None
    roe_pct: Optional[float] = None
    profit_margin_pct: Optional[float] = None
    revenue_growth_pct: Optional[float] = None
    earnings_growth_pct: Optional[float] = None
    debt_to_equity: Optional[float] = None          # ratio, 0.45 means 45%
    free_cash_flow: Optional[float] = None
    dividend_yield_pct: Optional[float] = None


class NewsItem(BaseModel):
    published: str
    title: str
    source: str


class NewsData(BaseModel):
    query: str
    items: list[NewsItem] = Field(default_factory=list)


# ---------- Risk engine output ----------

class RuleResult(BaseModel):
    name: str
    status: Literal["pass", "fail", "skipped"]
    detail: str                                      # why it passed, failed or was skipped


class RiskVerdict(BaseModel):
    result: Literal["fits", "borderline", "does_not_fit"]
    score: Optional[float] = None                    # 0 to 100, None if too little data
    rules_evaluated: int
    rules_total: int
    rules: list[RuleResult] = Field(default_factory=list)
    summary: str = ""