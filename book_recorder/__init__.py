"""Order book recorder for Polymarket: the data the Smart pod's research needs and nobody sells.

Polymarket keeps no order book history. This package records it:

  markets.py  picks the markets to follow (Gamma API, most active first)
  ws.py       subscribes to the CLOB websocket market channel and writes every message as it came
  store.py    one gzip JSON-lines file per UTC day, appended safely across restarts
  replay.py   rebuilds L2 books and the trade tape from those files, for research and checks
  trades.py   downloads historical trades with their wallets from the Data API v2

Nothing here needs a key or a wallet: every source is public.
"""
