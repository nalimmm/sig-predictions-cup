"""Shadow and real execution of the Smart market maker.

  shadow  live books from Polymarket's websocket, the same quoting logic as the paper market
          maker, and the order layer that would run in real mode, with a dry venue: every order
          is built, checked and journaled, nothing is signed or sent. Fills are simulated as in
          the paper market maker.
  live    the same code with the CLOB venue: orders are signed with the wallet key and sent.
          Refuses to start unless the owner armed it (see guard.Arming), the key is present, the
          geoblock check answers not blocked and the wallet's committed capital is within budget.

Nothing here prints or stores the wallet address or the key.
"""
