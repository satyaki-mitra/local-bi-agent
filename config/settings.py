# DEPENDENCIES
from typing import List
from pydantic import SecretStr
from pydantic import computed_field      
from pydantic import field_validator
from pydantic import model_validator      
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class Settings(BaseSettings):
    """
    Main application settings loaded from environment / .env file
    """
    # Environment
    environment                   : str        = "development"         # development | production | staging

    # Ollama
    ollama_host                   : str        = "http://ollama:11434"
    ollama_model                  : str        = "llama3:8b"
    ollama_temperature            : float      = 0.0
    ollama_max_tokens             : int        = 8192

    # Database – Health (PostgreSQL connection)
    db_health_host                : str        = "postgres-health"
    db_health_port                : int        = 5432                  # PostgreSQL port
    db_health_name                : str        = "health_db"
    db_health_user                : str        = "readonly_user"
    db_health_password            : SecretStr

    # Database – Finance
    db_finance_host               : str        = "postgres-finance"
    db_finance_port               : int        = 5432
    db_finance_name               : str        = "finance_db"
    db_finance_user               : str        = "readonly_user"
    db_finance_password           : SecretStr

    # Database – Sales
    db_sales_host                 : str        = "postgres-sales"
    db_sales_port                 : int        = 5432
    db_sales_name                 : str        = "sales_db"
    db_sales_user                 : str        = "readonly_user"
    db_sales_password             : SecretStr

    # Database – IoT
    db_iot_host                   : str        = "postgres-iot"
    db_iot_port                   : int        = 5432
    db_iot_name                   : str        = "iot_db"
    db_iot_user                   : str        = "readonly_user"
    db_iot_password               : SecretStr

    # Admin
    db_admin_user                 : str        = "postgres"
    db_admin_password             : SecretStr

    # FastAPI
    fastapi_host                  : str        = "0.0.0.0"
    fastapi_port                  : int        = 8001
    fastapi_workers               : int        = 2
    fastapi_reload                : bool       = False

    # Chainlit
    chainlit_host                 : str        = "0.0.0.0"
    chainlit_port                 : int        = 8000
    chainlit_allow_origins        : List[str]  = ["http://localhost:8000"]

    # Security
    enable_pii_redaction          : bool       = True
    max_sql_rows                  : int        = 10000
    sql_timeout_seconds           : int        = 30
    allow_only_select             : bool       = True
    db_ssl_mode                   : str        = "disable"             # "disable" | "require" | "verify-full"

    # Gateway HTTP server ports (db_gateway layer)
    db_base_host                  : str        = "localhost"           # set to "backend" inside Docker Compose
    gateway_health_port           : int        = 3001
    gateway_finance_port          : int        = 3002
    gateway_sales_port            : int        = 3003
    gateway_iot_port              : int        = 3004

    # Agent
    max_agent_retries             : int        = 3
    max_reasoning_steps           : int        = 10
    enable_reasoning_trace        : bool       = True

    # Rate limiting
    rate_limit_queries_per_minute : int        = 60
    max_query_length              : int        = 1000

    # Logging
    log_level                     : str        = "INFO"
    log_format                    : str        = "json"
    enable_request_logging        : bool       = True

    # Evaluation
    deepeval_enabled              : bool       = True
    deepeval_evaluator_model      : str        = "ollama/deepseek-r1:8b"
    golden_dataset_path           : str        = "/evaluation/golden_dataset.json"

    # Code Execution
    enable_code_sandbox           : bool       = True
    code_execution_timeout        : int        = 60
    allowed_imports               : List[str]  = ["os", "sys", "pandas", "numpy", "matplotlib", "seaborn"]

    # Feature flags
    enable_visualization          : bool       = True
    enable_cross_db_joins         : bool       = False                 # set True in .env to enable cross-domain queries
    enable_streaming_response     : bool       = False

    # Session History
    session_history_enabled       : bool       = True                  # set False to disable in-memory history
    session_history_max_turns     : int        = 20                    # max stored turns per session
    session_context_turns         : int        = 5                     # turns injected into LLM prompts

    # Cross-DB
    max_cross_db_domains          : int        = 2                     # max simultaneous domains per query

    # Export
    export_dir                    : str        = "temp/exports"
    export_cleanup_days           : int        = 7
    export_cleanup_interval_hours : int        = 24

    # Visualization output quality
    viz_dpi                       : int        = 100
    viz_figure_width              : float      = 10.0
    viz_figure_height             : float      = 6.0

    # LLM analyst data injection limit
    analyst_max_rows_in_prompt    : int        = 50

    # Outlier detection
    outlier_default_method        : str        = "iqr"

    # Frontend backend host
    backend_host                  : str        = "localhost"

    # Export temp dir for Chainlit file serving
    chainlit_temp_export_dir      : str        = "/tmp/localgenbi_exports"

    # DB request timeouts
    db_schema_timeout_seconds     : int        = 10
    db_query_timeout_seconds      : int        = 60

    # DB connection pool tuning
    db_pool_min_size              : int        = 2
    db_pool_max_size              : int        = 10
    db_pool_max_inactive_lifetime : float      = 60.0
    db_pool_max_queries           : int        = 50000


    # Validators 
    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "production", "staging"}

        if v not in allowed:
            raise ValueError(f"environment must be one of {allowed}, got '{v}'")

        return v


    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

        if v.upper() not in valid:
            raise ValueError(f"log_level must be one of {valid}, got '{v}'")

        return v.upper()

    
    @field_validator("allowed_imports", mode = "before")
    @classmethod
    def parse_allowed_imports(cls, v):
        if isinstance(v, str):
            return [item.strip() for item in v.split(",")]
        
        return v


    @field_validator("fastapi_workers")
    @classmethod
    def validate_workers(cls, v: int) -> int:
        if (v < 1):
            raise ValueError("fastapi_workers must be >= 1")

        return v


    @model_validator(mode = "after")
    def validate_production_ssl(self) -> "Settings":
        if ((self.environment == "production") and (self.db_ssl_mode == "disable")):
            raise ValueError("db_ssl_mode='disable' is not permitted in production. Set DB_SSL_MODE=require or DB_SSL_MODE=verify-full in your environment.")

        return self


    @model_validator(mode = "after")
    def validate_reload_workers(self) -> "Settings":
        if (self.fastapi_reload and (self.fastapi_workers > 1)):
            raise ValueError("fastapi_reload=True is incompatible with fastapi_workers > 1. Set FASTAPI_WORKERS=1 when using reload mode.")

        return self

    
    @field_validator("chainlit_allow_origins", mode = "before")
    @classmethod
    def parse_origins(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]

        return v


    # Pydantic-settings config
    model_config = SettingsConfigDict(env_file          = ".env",
                                      env_file_encoding = "utf-8",
                                      case_sensitive    = False,
                                     )


    # Computed fields
    @computed_field
    @property
    def backend_url(self) -> str:
        """
        Fully-qualified backend URL: override via BACKEND_HOST env var (e.g. 'backend' inside Docker Compose)
        
        - Usage: settings.backend_url → 'http://localhost:8001'
        """
        return f"http://{self.backend_host}:{self.fastapi_port}"


    # Helpers
    def get_db_url(self, db: str) -> str:
        """
        Build a PostgreSQL DSN for the given domain name
        
        - db: 'health' | 'finance' | 'sales' | 'iot'
        - Returns: 'postgresql://user:pass@host:5432/dbname'
        """
        mapping = {"health"  : (self.db_health_host, self.db_health_port, self.db_health_name, self.db_health_user, self.db_health_password),
                   "finance" : (self.db_finance_host, self.db_finance_port, self.db_finance_name, self.db_finance_user, self.db_finance_password),
                   "sales"   : (self.db_sales_host, self.db_sales_port, self.db_sales_name, self.db_sales_user, self.db_sales_password),
                   "iot"     : (self.db_iot_host, self.db_iot_port, self.db_iot_name, self.db_iot_user, self.db_iot_password),
                  }

        if db not in mapping:
            raise ValueError(f"Unknown database domain: '{db}'. Valid: {list(mapping)}")

        host, port, name, user, pwd = mapping[db]
        return f"postgresql://{user}:{pwd.get_secret_value()}@{host}:{port}/{name}"



# GLOBAL INSTANCE
settings = Settings()