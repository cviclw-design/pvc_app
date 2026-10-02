import os
from datetime import datetime
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from .config import config_by_name

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"


def create_app(config_name="dev"):
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(config_by_name[config_name])

    db.init_app(app)
    login_manager.init_app(app)

    if not app.debug or os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from .admin import run_backup_logic
            def run_backup():
                with app.app_context():
                    run_backup_logic()
            scheduler = BackgroundScheduler()
            # Schedule backup every day at 2:00 AM
            scheduler.add_job(func=run_backup, trigger="cron", hour=2, minute=0)
            scheduler.start()
            import atexit
            atexit.register(lambda: scheduler.shutdown())
        except ImportError:
            app.logger.warning("apscheduler not installed. Auto-backup disabled.")

    from .models import User  # noqa: F401

    from .auth import auth_bp
    from .admin import admin_bp
    from .views import main_bp
    from .pvc import pvc_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(main_bp)
    app.register_blueprint(pvc_bp, url_prefix="/pvc")

    @app.errorhandler(404)
    def not_found(e):
        return "Page not found", 404

    @app.errorhandler(500)
    def server_error(e):
        return "Internal server error", 500

    return app