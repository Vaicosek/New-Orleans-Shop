"""New Orleans web -- aiohttp, plain f-string templating, no Jinja.

Three audiences, one shell (see CONTRACT.md section 10):
  storefront/inventory  public, no session, must answer when the bot is down
  account           customer, Discord OAuth2
  ledger            staff only

No module in this package may reference any of the Discord-only tables
or vocabulary CONTRACT.md section 9 walls off -- see
tests/test_no_wagering_on_web.py.
"""
