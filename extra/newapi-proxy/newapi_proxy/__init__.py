"""newapi-proxy: the only path from the PaaS Odoo pods to new-api.

Design: work/paas-llm-tab-1016/DESIGN.md §1.4 (ticket LLM-01a). The proxy
verifies an HMAC signature from Odoo, canonicalises and whitelists every
request, injects the service-account PATs it alone holds, overwrites the
client IP with a synthetic one and never retries. See README.md for the wire
contract.
"""

__version__ = "0.1.0"
