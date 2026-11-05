import os, sys
sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["AWS_ACCESS_KEY_ID"] = "testing"          # never real credentials in tests
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
    os.environ.pop(k, None)
