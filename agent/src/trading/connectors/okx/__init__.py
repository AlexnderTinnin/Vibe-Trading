"""OKX trading connector.

Account/market access and order execution use the optional ``python-okx`` SDK
over the ``broker_sdk`` transport. Generic spot writes retain the existing
paper/live behavior; explicitly typed perpetual writes are Phase-1 Demo only.

Paper-vs-live separation is namespace + header based: OKX demo (paper) keys live
in a separate key namespace and the ``x-simulated-trading`` header (driven by the
SDK ``flag`` argument — ``"1"`` demo, ``"0"`` live) selects the environment. OKX
returns NO response field echoing demo/live, so there is no hard self-verifying
guard; the discriminator is the configured ``flag`` plus best-effort UID pinning
(``expected_uid``). The selected profile (and thus the flag) is recorded as
``paper`` on every payload and never flipped implicitly.
"""
