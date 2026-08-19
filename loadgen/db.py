"""pyodbc connection helpers."""
from __future__ import annotations

import pyodbc

from .config import TargetDB

DRIVER = "ODBC Driver 18 for SQL Server"


def connection_string(target: TargetDB, database: str = "master") -> str:
    parts = [
        f"DRIVER={{{DRIVER}}}",
        f"SERVER={target.host},{target.port}",
        f"DATABASE={database}",
        f"UID={target.user}",
        f"PWD={target.password}",
        f"Encrypt={'yes' if target.encrypt else 'no'}",
        f"TrustServerCertificate={'yes' if target.trust_server_certificate else 'no'}",
        f"LoginTimeout={target.login_timeout}",
    ]
    return ";".join(parts)


def connect(target: TargetDB, database: str = "master", autocommit: bool = True) -> pyodbc.Connection:
    conn = pyodbc.connect(connection_string(target, database), autocommit=autocommit)
    return conn
