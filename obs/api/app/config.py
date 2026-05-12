from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://agent_obs:changeme@localhost:5433/agent_obs"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    mhxy_executor_status_file: str = "/logs/omnibot/mhxy/executor_status.json"
    mhxy_instances_file: str = "/runtime/mhxy/config/instances.json"

    class Config:
        env_file = ".env"


settings = Settings()
