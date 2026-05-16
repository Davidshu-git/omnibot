from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://agent_obs:changeme@localhost:5433/agent_obs"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    mhxy_executor_status_file: str = "/logs/omnibot/mhxy/executor_status.json"
    mhxy_instances_file: str = "/runtime/mhxy/config/instances.json"
    mhxy_executor_url: str = "http://192.168.100.149:8765"
    obs_bot_chat_token: str = ""
    stock_bot_chat_url: str = "http://192.168.1.100:8810"
    ehs_bot_chat_url: str = "http://192.168.1.100:8811"
    mhxy_bot_chat_url: str = "http://192.168.1.100:8812"

    class Config:
        env_file = ".env"


settings = Settings()
