"""The Carpet: a rotation screener for Hyperliquid perps.

A second, separate tool beside the memecoin collector. It reads public market data
from Hyperliquid's info endpoint and describes how a basket of names is rotating
against the basket -- which quadrant each name sits in, whether it is rich or cheap
to the group, how stretched its pair spreads are, and whether its volume is running
hot or thin. ``docs/carpet.md`` records how each reading was reverse-engineered
from the screenshots it is modelled on, and which parts are inference.

It describes the tape. It does not forecast it: no reading here has been tested
for predictive value, and there is no code path from this package to an order
(hard rule 4).
"""
