import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-in-prod")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "mysql+pymysql://root@localhost/pvc_db2"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Path to the mysqldump executable.
    # On Windows with XAMPP, this is typically: 'C:/xampp/mysql/bin/mysqldump.exe'
    # Using forward slashes is recommended to avoid backslash issues.
    MYSQLDUMP_PATH = os.environ.get("MYSQLDUMP_PATH", "C:/xampp/mysql/bin/mysqldump.exe")


class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False


class ProductionConfig(Config):
    DEBUG = False
    TESTING = False


config_by_name = {
    "dev": DevelopmentConfig,
    "prod": ProductionConfig,
}