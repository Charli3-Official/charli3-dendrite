"""Liqwid V2 transaction builders on the shared lending transaction seam.

``errors.py`` holds the Liqwid build errors and ``terms.py`` the pure arithmetic
of every Liqwid V2 user flow: loan settlement, the per-action loan records and
action-UTxO deltas, and supply and redeem amounts. ``resolve.py`` reads from the
chain what a transaction spends and references: reference scripts, action UTxOs,
loans, market records and oracle feeds; ``legs.py`` holds the composable pieces
a transaction is built from.
"""
