"""Application configuration, loaded from environment / .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- app -------------------------------------------------------------
    app_name: str = "Pharmacy POS"
    version: str = "0.1.0"
    api_v1_prefix: str = "/api/v1"
    environment: str = "development"
    debug: bool = True
    display_timezone: str = "Asia/Kolkata"

    # --- database --------------------------------------------------------
    # sqlite:///./pharmacy.db  |  postgresql+psycopg://user:pass@host:5432/db
    database_url: str = "sqlite:///./pharmacy.db"
    sql_echo: bool = False

    # --- security --------------------------------------------------------
    secret_key: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # --- session cookie --------------------------------------------------
    # The refresh token rides in an httpOnly cookie so that injected script
    # cannot read it — the whole point of moving it off localStorage. None of
    # this ever holds a password: passwords are argon2 hashes in the database
    # and are never written anywhere a browser can reach.
    cookie_name: str = "pharmacy_session"
    # Must be True wherever the app is served over HTTPS. Left False so the
    # http://127.0.0.1 dev setup works; a Secure cookie is simply dropped by
    # the browser on a plain-http origin, which would look like a broken login.
    cookie_secure: bool = False
    # `lax` blocks the cookie on cross-site POSTs, which is what keeps CSRF
    # off the refresh endpoint. It also means the SPA and the API must be on
    # the same site: use 127.0.0.1 for both, or localhost for both — mixing
    # them makes the request cross-site and the cookie is not sent at all.
    cookie_samesite: str = "lax"
    # Leave unset for a host-only cookie. Set to ".pharmacy.example" only when
    # the SPA and API live on different sub-domains of one site.
    cookie_domain: str | None = None
    # How long "remember me" keeps someone signed in. Without it the cookie
    # is a session cookie and dies with the browser.
    remember_me_days: int = 30

    # --- cors ------------------------------------------------------------
    # comma-separated so .env stays human-editable
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- serving the SPA --------------------------------------------------
    # Off in development, where Vite serves the app with hot reload and proxies
    # the API. On in the native Windows deployment, where it replaces a second
    # web server: one process on one port means the page and the API are the
    # same origin, so there is no CORS to configure and the httpOnly session
    # cookie behaves without a `SameSite` exception.
    #
    # This is not the app rendering HTML — the API still answers only in JSON
    # (CLAUDE.md, Stack). It hands over files Vite has already built. The
    # alternative on a machine without Docker is a second service to install,
    # start at boot and keep patched, for no gain.
    serve_spa: bool = False
    #: Where `npm run build` puts its output. Relative paths resolve against
    #: the repository root, not the working directory the service happens to
    #: start in.
    spa_dist_dir: str = "../frontend/dist"

    # --- document numbers -------------------------------------------------
    # Where the customer-facing invoice series begins. A shop moving off a
    # paper book does not want to start again at 1 beside numbers it has
    # already issued, so the series opens high enough to sit clear of them.
    #
    # Only the sale invoice: returns, credit notes and purchases keep their
    # own series from 1. Numbers are zero-padded to 8 digits and the column
    # is CHAR(8), so this must be an 8-digit number — 10000001 leaves just
    # under 90 million invoices before the series is exhausted.
    invoice_number_start: int = 10_000_001

    # --- seed ------------------------------------------------------------
    seed_store_name: str = "My Pharmacy"
    seed_store_state_code: str = "18"
    # Must be a real-looking domain: EmailStr rejects reserved TLDs such as
    # .local, .test, .example and .invalid, so a `.local` admin could never
    # sign in.
    seed_admin_email: str = "admin@pharmacy.in"
    seed_admin_password: str = "Admin@123"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def spa_dist_path(self) -> Path:
        """The built SPA, as an absolute path.

        Resolved against the backend directory rather than the process's
        working directory: a Windows service starts in `C:\\Windows\\System32`
        and a relative path there finds nothing.
        """
        candidate = Path(self.spa_dist_dir)
        if not candidate.is_absolute():
            candidate = BACKEND_DIR / candidate
        return candidate.resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
