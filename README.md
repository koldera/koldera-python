# koldera

Python SDK for [koldera-core](https://koldera-accessibility.dev): PDF/UA checks and fixes, HTML and structured
documents to tagged PDF, and web scans. A synchronous and an asynchronous client share one implementation.

```sh
pip install koldera
```

```python
from koldera import KolderaClient

with KolderaClient(base_url="https://koldera.example.com", token="...") as client:
    job = client.pdf.create_job("report.pdf", mode="fix", wait=60, until_finished=True)
    print(job.state, job.final_validation)
```

```python
import asyncio
from koldera import AsyncKolderaClient


async def main() -> None:
    async with AsyncKolderaClient() as client:  # KOLDERA_URL and KOLDERA_TOKEN
        job = await client.web.create_scan("https://example.com", wait=30)
        print(job.state)


asyncio.run(main())
```

The server token stays on your backend. Mint a short-lived session for a browser with
`client.client_sessions.create(allowed_origin=...)`.

Licensed under Apache-2.0. Full documentation follows with the first release.
