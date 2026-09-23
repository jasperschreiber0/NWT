"""Versioned paper research universe; no account sizing or order permissions."""
SYMBOLS = 'SPY QQQ IWM DIA GLD SLV TLT HYG XLE XLF XLK XLC XLV XLI XLP XLY XLU XLB XLRE SMH XBI KRE ARKK VGK FEZ EWU EWA EEM FXI AAPL MSFT NVDA AMZN META GOOGL TSLA AMD COIN PLTR BHP SAFX'.split()
OPTION_UNDERLYINGS = 'SPY QQQ IWM GLD TLT XLE XLF SMH AAPL NVDA TSLA AMD'.split()
VERSION = '20260924-paper-discovery-v2'


def paged(get, url, params, field, max_pages=30):
    """Never present a partial response as a complete research dataset."""
    result = {}
    token = None
    seen = set()
    for _ in range(max_pages):
        page = get(url, dict(params, **({'page_token': token} if token else {})))
        if field not in page:
            raise RuntimeError('Required market dataset unavailable: ' + field)
        for symbol, value in (page[field] or {}).items():
            if isinstance(value, list): result.setdefault(symbol, []).extend(value)
            else: result[symbol] = value
        token = page.get('next_page_token')
        if not token: return {field: result, 'next_page_token': None}
        if token in seen: raise RuntimeError('Repeated market pagination token')
        seen.add(token)
    raise RuntimeError('Market pagination limit reached; incomplete data rejected')
