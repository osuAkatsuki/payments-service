import os

from dotenv import load_dotenv

load_dotenv()


def read_bool(value: str) -> bool:
    return value.lower() in ("1", "true")


APP_ENV = os.environ["APP_ENV"]
APP_HOST = os.environ["APP_HOST"]
APP_PORT = int(os.environ["APP_PORT"])

CODE_HOTRELOAD = read_bool(os.environ["CODE_HOTRELOAD"])

DB_DIALECT = os.environ["DB_DIALECT"]
DB_USER = os.environ["DB_USER"]
DB_HOST = os.environ["DB_HOST"]
DB_PORT = int(os.environ["DB_PORT"])
DB_NAME = os.environ["DB_NAME"]
DB_DRIVER = os.environ["DB_DRIVER"]
DB_PASS = os.environ["DB_PASS"]
INITIALLY_AVAILABLE_DB = os.environ["INITIALLY_AVAILABLE_DB"]

PAYPAL_BUSINESS_EMAIL = os.environ["PAYPAL_BUSINESS_EMAIL"]

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]

# temp/feature flags
SHOULD_WRITE_TO_USERS_DB = read_bool(os.environ["SHOULD_WRITE_TO_USERS_DB"])
SHOULD_ENFORCE_UNIQUE_PAYMENTS = read_bool(os.environ["SHOULD_ENFORCE_UNIQUE_PAYMENTS"])
SHOULD_REQUIRE_IPN_VERIFICATION = read_bool(
    os.environ["SHOULD_REQUIRE_IPN_VERIFICATION"],
)
