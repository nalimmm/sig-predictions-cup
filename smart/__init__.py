"""Smart pod: systematic strategies on Polymarket, paper first (see smart/engine.py).

S1, reversion of the transient impact after a sweep of the book, is the first strategy. It reads
the books that book_recorder writes, so a backtest and the paper trader share one code path.
"""
