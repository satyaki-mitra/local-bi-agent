# DEPENDENCIES
import sys
import asyncio
import asyncpg
import structlog
from pathlib import Path
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import settings


logger = structlog.get_logger()


# Connection helper
async def connect_with_retry(host: str, port: int, database: str, user: str, password: SecretStr, max_retries: int = 10, base_delay: float = 2.0) -> asyncpg.Connection:
    """
    Connect to a PostgreSQL database with exponential back-off: accepts password as SecretStr and unwraps it internally
    """
    for attempt in range(1, max_retries + 1):
        try:
            conn = await asyncpg.connect(host     = host,
                                         port     = port,
                                         database = database,
                                         user     = user,
                                         password = password.get_secret_value(),
                                        )

            logger.info("Database connected",
                        database = database,
                        attempt  = attempt,
                       )

            return conn

        except Exception as e:
            if (attempt == max_retries):
                logger.error("Database connection failed after max retries",
                             database    = database,
                             max_retries = max_retries,
                             error       = str(e),
                            )
                raise

            delay = min(base_delay * (2 ** (attempt - 1)), 30.0)

            logger.warning("Database not ready, retrying",
                           database = database,
                           attempt  = attempt,
                           retries  = max_retries,
                           delay_s  = delay,
                          )
            await asyncio.sleep(delay)


def _escape_password(password: str) -> str:
    """
    Escape a password string for safe interpolation into ALTER USER SQL
    """
    escaped = password.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


# Readonly user
async def create_readonly_user(conn    : asyncpg.Connection,
                                db_name : str,
                                password: SecretStr,
                               ) -> None:
    """
    Create (or confirm) the read-only application user and grant SELECT on every current and future table in the public schema

    IMPORTANT: Must NOT be called concurrently across databases readonly_user is a single cluster-level role. 
    Concurrent ALTER USER calls all write to the same pg_authid row and cause 'tuple concurrently updated'.
    Always call via setup_readonly_user_all_dbs() which runs sequentially.
    """
    _KNOWN_DB_NAMES = {"health_db", "finance_db", "sales_db", "iot_db"}

    if db_name not in _KNOWN_DB_NAMES:
        raise ValueError(f"Unexpected db_name: {db_name!r}")

    try:
        # Create user if it does not exist
        await conn.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT FROM pg_user WHERE usename = 'readonly_user') THEN
                    CREATE USER readonly_user;
                END IF;
            END
            $$;
        """)

        # ALTER USER does not support $1 parameters in PostgreSQL — DDL only supports literal values. Password is escaped manually via _escape_password()
        escaped_pw = _escape_password(password.get_secret_value())
        await conn.execute(f"ALTER USER readonly_user WITH PASSWORD {escaped_pw}")

        # db_name validated against whitelist above — safe to interpolate
        await conn.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO readonly_user')
        await conn.execute("GRANT USAGE ON SCHEMA public TO readonly_user")
        await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_user")
        await conn.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly_user")

        logger.info("Read-only user configured", database = db_name)

    except Exception as e:
        logger.error("Failed to create readonly user", database = db_name, error = str(e))
        raise


# Per-domain schema setup
# To add a table: paste a new CREATE TABLE IF NOT EXISTS block inside the
# triple-quoted string. The readonly user grant runs after all tables are
# created so it covers everything automatically.
async def setup_health_db() -> None:
    conn = await connect_with_retry(host     = settings.db_health_host,
                                    port     = settings.db_health_port,
                                    database = settings.db_health_name,
                                    user     = settings.db_admin_user,
                                    password = settings.db_admin_password,
                                   )
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS patient_history (
                patient_id         SERIAL PRIMARY KEY,
                age                INTEGER,
                gender             VARCHAR(10),
                risk_score         DECIMAL(5, 2),
                chronic_conditions TEXT[]
            );

            CREATE TABLE IF NOT EXISTS claims (
                claim_id       SERIAL PRIMARY KEY,
                patient_id     INTEGER REFERENCES patient_history(patient_id),
                diagnosis_code VARCHAR(10),
                claim_amount   DECIMAL(10, 2),
                claim_date     DATE,
                status         VARCHAR(20)
            );

            CREATE TABLE IF NOT EXISTS procedures (
                procedure_id   SERIAL PRIMARY KEY,
                claim_id       INTEGER REFERENCES claims(claim_id),
                procedure_code VARCHAR(10),
                procedure_date DATE,
                cost           DECIMAL(10, 2)
            );

            -- ── Add new Health tables below this line ─────────────────────
        """)

        logger.info("Health database schema ready")

    finally:
        await conn.close()


async def setup_finance_db() -> None:
    conn = await connect_with_retry(host     = settings.db_finance_host,
                                    port     = settings.db_finance_port,
                                    database = settings.db_finance_name,
                                    user     = settings.db_admin_user,
                                    password = settings.db_admin_password,
                                   )
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id   SERIAL PRIMARY KEY,
                customer_id      INTEGER NOT NULL,
                amount           DECIMAL(10, 2),
                transaction_date TIMESTAMP,
                status           VARCHAR(20),
                payment_method   VARCHAR(50)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                subscription_id SERIAL PRIMARY KEY,
                customer_id     INTEGER NOT NULL,
                plan_type       VARCHAR(50),
                monthly_fee     DECIMAL(10, 2),
                start_date      DATE,
                renewal_date    DATE,
                status          VARCHAR(20)
            );

            CREATE TABLE IF NOT EXISTS payment_failures (
                failure_id     SERIAL PRIMARY KEY,
                customer_id    INTEGER NOT NULL,
                transaction_id INTEGER,
                failure_reason VARCHAR(100),
                failure_date   TIMESTAMP,
                retry_count    INTEGER DEFAULT 0
            );

            -- ── Add new Finance tables below this line ────────────────────
        """)

        logger.info("Finance database schema ready")

    finally:
        await conn.close()


async def setup_sales_db() -> None:
    conn = await connect_with_retry(host     = settings.db_sales_host,
                                    port     = settings.db_sales_port,
                                    database = settings.db_sales_name,
                                    user     = settings.db_admin_user,
                                    password = settings.db_admin_password,
                                   )
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                lead_id      SERIAL PRIMARY KEY,
                lead_name    VARCHAR(100),
                email        VARCHAR(100),
                source       VARCHAR(50),
                created_date DATE,
                status       VARCHAR(20)
            );

            CREATE TABLE IF NOT EXISTS opportunities (
                opportunity_id    SERIAL PRIMARY KEY,
                lead_id           INTEGER REFERENCES leads(lead_id),
                opportunity_value DECIMAL(12, 2),
                probability       DECIMAL(5, 2),
                close_date        DATE,
                stage             VARCHAR(50)
            );

            CREATE TABLE IF NOT EXISTS sales_reps (
                rep_id            SERIAL PRIMARY KEY,
                rep_name          VARCHAR(100),
                region            VARCHAR(50),
                total_sales       DECIMAL(12, 2),
                quota             DECIMAL(12, 2),
                performance_score DECIMAL(5, 2)
            );

            -- ── Add new Sales tables below this line ──────────────────────
        """)

        logger.info("Sales database schema ready")

    finally:
        await conn.close()


async def setup_iot_db() -> None:
    conn = await connect_with_retry(host     = settings.db_iot_host,
                                    port     = settings.db_iot_port,
                                    database = settings.db_iot_name,
                                    user     = settings.db_admin_user,
                                    password = settings.db_admin_password,
                                   )
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_steps (
                record_id   SERIAL PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                date        DATE,
                step_count  INTEGER,
                distance_km DECIMAL(5, 2)
            );

            CREATE TABLE IF NOT EXISTS heart_rate_avg (
                record_id          SERIAL PRIMARY KEY,
                user_id            INTEGER NOT NULL,
                date               DATE,
                avg_heart_rate     INTEGER,
                resting_heart_rate INTEGER
            );

            CREATE TABLE IF NOT EXISTS sleep_hours (
                record_id            SERIAL PRIMARY KEY,
                user_id              INTEGER NOT NULL,
                date                 DATE,
                sleep_duration_hours DECIMAL(4, 2),
                sleep_quality_score  INTEGER
            );

            -- ── Add new IoT tables below this line ────────────────────────
        """)

        logger.info("IoT database schema ready")

    finally:
        await conn.close()


async def setup_readonly_user_all_dbs() -> None:
    """
    Grant readonly_user access to all four databases sequentially: must be sequential — readonly_user is a single cluster-level role

    Concurrent ALTER USER calls across databases write to the same pg_authid row and cause 'tuple concurrently updated' errors
    """
    domains = [(settings.db_health_host, settings.db_health_port, settings.db_health_name, settings.db_health_password),
               (settings.db_finance_host, settings.db_finance_port, settings.db_finance_name, settings.db_finance_password),
               (settings.db_sales_host, settings.db_sales_port, settings.db_sales_name, settings.db_sales_password),
               (settings.db_iot_host, settings.db_iot_port, settings.db_iot_name, settings.db_iot_password),
              ]

    for host, port, db_name, db_password in domains:
        conn = await connect_with_retry(host     = host,
                                        port     = port,
                                        database = db_name,
                                        user     = settings.db_admin_user,
                                        password = settings.db_admin_password,
                                       )
        try:
            await create_readonly_user(conn, db_name, db_password)
        finally:
            await conn.close()


# Entry point
async def main() -> None:
    logger.info("Starting database schema setup...")

    # Step 1 — create all schemas concurrently (independent databases, no shared state)
    await asyncio.gather(setup_health_db(),
                         setup_finance_db(),
                         setup_sales_db(),
                         setup_iot_db(),
                        )

    # Step 2 — grant readonly_user access sequentially (shared pg_authid row — concurrent ALTER USER causes 'tuple concurrently updated')
    await setup_readonly_user_all_dbs()

    logger.info("All database schemas initialised successfully")


if __name__ == "__main__":
    asyncio.run(main())