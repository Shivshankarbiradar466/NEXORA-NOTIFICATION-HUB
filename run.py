import os
from dotenv import load_dotenv

load_dotenv()

# OAuthlib permits plain HTTP only when explicitly opted into for local development.
# Never set NEXORA_ALLOW_INSECURE_OAUTH in production.
if os.getenv("NEXORA_ALLOW_INSECURE_OAUTH", "").lower() in {"1", "true", "yes"}:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
else:
    os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
from app import create_app
app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
