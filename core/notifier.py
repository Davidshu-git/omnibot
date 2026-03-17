"""
邮件推送模块 - 负责发送盘后市场报告邮件。

本模块提供基于 SMTP 协议的邮件发送功能，支持 Markdown 格式内容渲染为 HTML。
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Final

import markdown
from tenacity import retry, stop_after_attempt, wait_exponential


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10)
)
def send_market_report_email(subject: str, markdown_content: str) -> bool:
    """
    发送市场报告邮件到指定收件人。

    Args:
        subject: 邮件主题。
        markdown_content: Markdown 格式的邮件正文内容。

    Returns:
        bool: 发送成功返回 True，失败返回 False。

    Raises:
        smtplib.SMTPException: SMTP 通信异常。
        ConnectionError: 网络连接错误。
        ValueError: 环境变量配置缺失。
    """
    smtp_server: str = os.getenv("SMTP_SERVER", "")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    sender_email: str = os.getenv("SENDER_EMAIL", "")
    sender_password: str = os.getenv("SENDER_PASSWORD", "")
    receiver_email: str = os.getenv("RECEIVER_EMAIL", "")

    if not all([smtp_server, sender_email, sender_password, receiver_email]):
        raise ValueError(
            "缺少必要的邮件配置环境变量："
            "SMTP_SERVER, SENDER_EMAIL, SENDER_PASSWORD, RECEIVER_EMAIL"
        )

    html_content: str = markdown.markdown(
        markdown_content,
        extensions=["tables", "fenced_code", "toc", "nl2br", "sane_lists"]
    )

    styled_html: str = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333333; max-width: 900px; margin: 0 auto; padding: 20px; }}
            h1, h2, h3 {{ color: #2c3e50; border-bottom: 1px solid #eaeaea; padding-bottom: 8px; margin-top: 24px; }}
            p {{ margin-top: 0; margin-bottom: 16px; }}
            ul, ol {{ margin-top: 8px; margin-bottom: 16px; padding-left: 28px; }}
            li {{ margin-bottom: 10px; }}
            li > p {{ margin-bottom: 6px; }}
            li > ul, li > ol {{ margin-top: 6px; margin-bottom: 0; }}
            table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 14px; }}
            th, td {{ padding: 12px 15px; border: 1px solid #dddddd; text-align: left; }}
            th {{ background-color: #f8f9fa; font-weight: bold; color: #2c3e50; }}
            tr:nth-child(even) {{ background-color: #fafafa; }}
            blockquote {{ border-left: 4px solid #e74c3c; margin: 0; padding-left: 15px; color: #555555; background-color: #fef9f9; padding: 10px; border-radius: 0 4px 4px 0; }}
            code {{ background-color: #f4f4f4; padding: 2px 4px; border-radius: 4px; font-family: Consolas, monospace; color: #d35400; }}
        </style>
    </head>
    <body>
        {html_content}
    </body>
    </html>
    """

    msg: MIMEMultipart = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email

    part_text: MIMEText = MIMEText(markdown_content, "plain", "utf-8")
    part_html: MIMEText = MIMEText(styled_html, "html", "utf-8")

    msg.attach(part_text)
    msg.attach(part_html)

    try:
        with smtplib.SMTP_SSL(smtp_server, smtp_port) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, [receiver_email], msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError as e:
        raise smtplib.SMTPAuthenticationError(f"SMTP 认证失败：{e}")
    except smtplib.SMTPConnectError as e:
        raise smtplib.SMTPConnectError(f"SMTP 连接失败：{e}")
    except smtplib.SMTPException as e:
        raise smtplib.SMTPException(f"SMTP 通信异常：{e}")
    except ConnectionError as e:
        raise ConnectionError(f"网络连接错误：{e}")
