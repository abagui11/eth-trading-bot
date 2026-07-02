"""Run: python -m dashboard"""

import uvicorn

from dashboard.app import create_app

app = create_app()

if __name__ == "__main__":
    import os

    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    uvicorn.run("dashboard.app:create_app", factory=True, host="0.0.0.0", port=port)
