"""Browser-session backfill (Hướng B): pull full chat history from the Teams
web client's internal chatsvc API, beyond what the local cache holds.

The pure request/parse logic lives in ``chatsvc`` and the pagination loop in
``backfill.collect_messages`` (both unit-tested). The Playwright session in
``session`` and the orchestration in ``backfill.backfill`` drive a real logged-in
browser and are run interactively by the user (VPN + MFA).
"""
