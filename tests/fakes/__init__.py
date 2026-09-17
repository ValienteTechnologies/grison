"""In-memory fake remotes (Ghostwriter GraphQL + BookStack REST) for grison's e2e tests.

Both fakes are exposed as ``httpx.MockTransport`` handlers so the real client classes
(``GhostwriterClient`` / ``BookStackClient``) talk to them exactly as they would to the
real servers — no grison code is aware it is running against a fake.
"""
