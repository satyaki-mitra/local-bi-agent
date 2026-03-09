# DEPENDENCIES
import sys
import random
import asyncio
import asyncpg
import structlog
from pathlib import Path
from datetime import datetime
from datetime import timedelta

sys.path.insert(0, str(Path(__file__).parent.parent))
from config.settings import settings


logger = structlog.get_logger()


# Health
async def populate_health_db() -> None:
    conn = await asyncpg.connect(host     = settings.db_health_host,
                                 port     = settings.db_health_port,
                                 database = settings.db_health_name,
                                 user     = settings.db_admin_user,
                                 password = settings.db_admin_password.get_secret_value(),
                                )
    try:
        # Truncate all health tables in dependency order (child tables first)
        await conn.execute("""
            TRUNCATE TABLE procedures, claims, patient_history
            RESTART IDENTITY CASCADE;
        """)

        # patient_history
        patients = [(random.randint(25, 75),
                     random.choice(["Male", "Female"]),
                     round(random.uniform(1.0, 9.9), 2),
                     [random.choice(["Diabetes", "Hypertension", "Asthma", "None"])],
                    )
                    for _ in range(100)
                   ]
        await conn.executemany("INSERT INTO patient_history (age, gender, risk_score, chronic_conditions) VALUES ($1, $2, $3, $4)",
                               patients,
                              )

        # claims
        base_date = datetime(2024, 1, 1).date()
        claims    = [(random.randint(1, 100),
                      f"D{random.randint(100, 999)}",
                      round(random.uniform(100, 50000), 2),
                      base_date + timedelta(days=random.randint(0, 365)),
                      random.choice(["Approved", "Pending", "Denied"]),
                     )
                     for _ in range(500)
                    ]
        await conn.executemany("INSERT INTO claims (patient_id, diagnosis_code, claim_amount, claim_date, status) VALUES ($1, $2, $3, $4, $5)",
                               claims,
                              )

        # procedures: each claim gets 0-2 associated procedure rows.
        procedure_codes = ["P001", "P002", "P003", "P004", "P005", "P006", "P007", "P008", "P009", "P010"]
        procedures      = list()

        for claim_id in range(1, 501):
            for _ in range(random.randint(0, 2)):
                claim_date     = base_date + timedelta(days=random.randint(0, 365))
                procedure_date = claim_date + timedelta(days=random.randint(0, 14))
                
                procedures.append((claim_id,
                                   random.choice(procedure_codes),
                                   procedure_date,
                                   round(random.uniform(50, 8000), 2),
                                 ))
        await conn.executemany("INSERT INTO procedures (claim_id, procedure_code, procedure_date, cost) VALUES ($1, $2, $3, $4)",
                               procedures,
                              )

        logger.info("Health database populated",
                    patients   = len(patients),
                    claims     = len(claims),
                    procedures = len(procedures),
                   )

        # Add INSERT blocks for new Health tables below this line 
    finally:
        await conn.close()


# Finance
async def populate_finance_db() -> None:
    conn = await asyncpg.connect(host     = settings.db_finance_host,
                                 port     = settings.db_finance_port,
                                 database = settings.db_finance_name,
                                 user     = settings.db_admin_user,
                                 password = settings.db_admin_password.get_secret_value(),
                                )
    try:
        # Truncate in FK-safe order (payment_failures references transactions)
        await conn.execute("""
            TRUNCATE TABLE payment_failures, subscriptions, transactions
            RESTART IDENTITY CASCADE;
        """)

        base_dt = datetime(2024, 1, 1)

        # transactions
        transactions = [(random.randint(1, 200),
                         round(random.uniform(10, 5000), 2),
                         base_dt + timedelta(days    = random.randint(0, 365),
                                             hours   = random.randint(0, 23),
                                             minutes = random.randint(0, 59)
                                            ),
                         random.choice(["Completed", "Pending", "Failed"]),
                         random.choice(["Credit Card", "Debit Card", "PayPal", "Bank Transfer"]),
                        )
                        for _ in range(1000)
                       ]
        await conn.executemany("INSERT INTO transactions (customer_id, amount, transaction_date, status, payment_method) VALUES ($1, $2, $3, $4, $5)",
                               transactions,
                              )

        # subscriptions 
        subscriptions = list()

        for customer_id in range(1, 201):
            start   = (base_dt + timedelta(days = random.randint(0, 300))).date()
            renewal = start + timedelta(days = 30)

            subscriptions.append((customer_id,
                                  random.choice(["Basic", "Premium", "Enterprise"]),
                                  round(random.uniform(9.99, 299.99), 2),
                                  start,
                                  renewal,
                                  random.choice(["Active", "Cancelled", "Expired"]),
                                ))
        await conn.executemany("INSERT INTO subscriptions (customer_id, plan_type, monthly_fee, start_date, renewal_date, status) VALUES ($1, $2, $3, $4, $5, $6)",
                               subscriptions,
                              )

        # payment_failures: Generate one failure row per transaction that has status = "Failed"
        failure_reasons  = ["Insufficient funds",
                            "Card expired",
                            "Invalid card number",
                            "Bank declined",
                            "Network timeout",
                            "Fraud suspected",
                           ]

        failed_txn_ids   = [i + 1 for i, txn in enumerate(transactions) if txn[3] == "Failed"]
        payment_failures = [(txn[0],                            # customer_id
                             txn_id,                            # transaction_id (1-based position)
                             random.choice(failure_reasons),
                             txn[2],                            # failure_date = transaction_date
                             random.randint(0, 3),              # retry_count
                            )
                            for txn_id, txn in ((i + 1, transactions[i])
                                                for i in range(len(transactions)))
                            if txn[3] == "Failed"
                           ]
        await conn.executemany("INSERT INTO payment_failures (customer_id, transaction_id, failure_reason, failure_date, retry_count) VALUES ($1, $2, $3, $4, $5)",
                               payment_failures,
                              )

        logger.info("Finance database populated",
                    transactions     = len(transactions),
                    subscriptions    = len(subscriptions),
                    payment_failures = len(payment_failures),
                   )

        # Add INSERT blocks for new Finance tables below this line

    finally:
        await conn.close()


# Sales
async def populate_sales_db() -> None:
    conn = await asyncpg.connect(host     = settings.db_sales_host,
                                 port     = settings.db_sales_port,
                                 database = settings.db_sales_name,
                                 user     = settings.db_admin_user,
                                 password = settings.db_admin_password.get_secret_value(),
                                )
    try:
        # Truncate in FK-safe order (opportunities references leads)
        await conn.execute("""
            TRUNCATE TABLE opportunities, leads, sales_reps
            RESTART IDENTITY CASCADE;
        """)

        base_date = datetime(2024, 1, 1).date()

        # leads
        leads = [(f"Lead {i}",
                  f"lead{i}@example.com",
                  random.choice(["Website", "Referral", "Cold Call", "Event"]),
                  base_date + timedelta(days=random.randint(0, 365)),
                  random.choice(["New", "Contacted", "Qualified", "Lost"]),
                 )
                 for i in range(1, 301)
                ]
        await conn.executemany("INSERT INTO leads (lead_name, email, source, created_date, status) VALUES ($1, $2, $3, $4, $5)",
                               leads,
                              )

        # opportunities
        opportunities = [(random.randint(1, 300),
                          round(random.uniform(1000, 100000), 2),
                          round(random.uniform(0.1, 0.9), 2),
                          base_date + timedelta(days=random.randint(30, 400)),
                          random.choice(["Prospecting", "Qualification", "Proposal",
                                         "Negotiation", "Closed Won"]),
                         )
                         for _ in range(150)
                        ]

        await conn.executemany("INSERT INTO opportunities (lead_id, opportunity_value, probability, close_date, stage) VALUES ($1, $2, $3, $4, $5)",
                               opportunities,
                              )

        # sales_reps
        reps = [(f"Rep {i}",
                 random.choice(["North", "South", "East", "West"]),
                 round(random.uniform(50000, 500000), 2),
                 round(random.uniform(100000, 600000), 2),
                 round(random.uniform(60, 100), 2),
                )
                for i in range(1, 21)
               ]

        await conn.executemany("INSERT INTO sales_reps (rep_name, region, total_sales, quota, performance_score) VALUES ($1, $2, $3, $4, $5)",
                               reps,
                              )

        logger.info("Sales database populated",
                    leads         = len(leads),
                    opportunities = len(opportunities),
                    sales_reps    = len(reps),
                   )

        # Add INSERT blocks for new Sales tables below this line

    finally:
        await conn.close()


# IoT
async def populate_iot_db() -> None:
    conn = await asyncpg.connect(host     = settings.db_iot_host,
                                 port     = settings.db_iot_port,
                                 database = settings.db_iot_name,
                                 user     = settings.db_admin_user,
                                 password = settings.db_admin_password.get_secret_value(),
                                )
    try:
        await conn.execute("""
            TRUNCATE TABLE daily_steps, heart_rate_avg, sleep_hours
            RESTART IDENTITY CASCADE;
        """)

        base_date             = datetime(2024, 1, 1).date()
        steps, hearts, sleeps = [], [], []

        for user_id in range(1, 51):
            for day in range(365):
                current_date = base_date + timedelta(days=day)

                steps.append((user_id,
                              current_date,
                              random.randint(2000, 15000),
                              round(random.uniform(1.5, 12.0), 2),
                            ))

                hearts.append((user_id,
                               current_date,
                               random.randint(60, 100),
                               random.randint(50, 70),
                             ))

                sleeps.append((user_id,
                               current_date,
                               round(random.uniform(4.0, 10.0), 2),
                               random.randint(50, 100),
                             ))

        await conn.executemany("INSERT INTO daily_steps (user_id, date, step_count, distance_km) VALUES ($1, $2, $3, $4)",
                               steps,
                              )

        await conn.executemany("INSERT INTO heart_rate_avg (user_id, date, avg_heart_rate, resting_heart_rate) VALUES ($1, $2, $3, $4)",
                               hearts,
                              )

        await conn.executemany("INSERT INTO sleep_hours (user_id, date, sleep_duration_hours, sleep_quality_score) VALUES ($1, $2, $3, $4)",
                               sleeps,
                              )

        logger.info("IoT database populated",
                    users          = 50,
                    days           = 365,
                    rows_per_table = len(steps),
                   )

        # Add INSERT blocks for new IoT tables below this line

    finally:
        await conn.close()


# Entry point
async def main() -> None:
    logger.info("Generating demo data...")

    # All four databases are populated concurrently: add new populate_<domain>_db() coroutines here as new databases are added
    await asyncio.gather(populate_health_db(),
                         populate_finance_db(),
                         populate_sales_db(),
                         populate_iot_db(),
                        )

    logger.info("Demo data generation complete")


if __name__ == "__main__":
    asyncio.run(main())