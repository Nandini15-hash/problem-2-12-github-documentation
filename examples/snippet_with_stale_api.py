# Code a developer copied from an old blog post / old docs.
import httpx

client = httpx.Client(cert=("client.pem", "client.key"), verify="/etc/ssl/ca.pem")
r = httpx.get("https://example.org", extensions={"timeout": {"connect": 1}})
